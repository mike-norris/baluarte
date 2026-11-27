# ruff: noqa: F811 (pytest fixtures imported from other test modules)
"""Change set records and acceptance (M14.6): rationale, who ran each write (never an approver),
controls, the evidence package, and the bulk workflow end to end with a partial failure and an
exception that stay visible."""

import csv
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_project import SETTINGS as GITLAB_SETTINGS
from test_evaluation_project import fresh, instance_id  # noqa: F401
from test_template_change_execution import (  # noqa: F401
    RAISE,
    SETS,
    GitLab,
    approved,
    as_admin,
    gitlab,
    no_network,
    run,
    secrets,
    writes_on,
)
from test_template_change_sets import API, SITE
from test_template_drift import (  # noqa: F401
    client,
    drift,
    gitlab_env,
    observe_approvals,
    project_id,
    user,
    world,
)

from baluarte.audit import details_digest
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, TemplateChangeRun, TemplateChangeSet
from baluarte.evidence import verify as verify_tool
from baluarte.policy.evaluation.runner import evaluate_all
from baluarte.templates.execute import ROLLBACK_CHANGE_SET

pytestmark = pytest.mark.db

WHY = "Bring the platform projects in line with the service template."


def refused(response: Any) -> str:
    assert response.status_code == 409, response.text
    return str(response.json()["detail"])


def test_a_rationale_is_required_and_never_changes(migrated_engine: Engine, world: int) -> None:
    analyst = client(migrated_engine, user(migrated_engine, "ana"), Role.ANALYST)
    body = {"title": "Raise", "selection": {"projects": [API]}, "settings": RAISE}

    assert analyst.post(SETS, json=body).status_code == 422
    assert analyst.post(SETS, json={**body, "rationale": "too short"}).status_code == 422
    created = analyst.post(SETS, json={**body, "rationale": WHY})
    assert created.status_code == 201, created.text
    assert created.json()["rationale"] == WHY

    with pytest.raises(DBAPIError, match="rationale cannot change"):
        with migrated_engine.begin() as connection:
            connection.execute(
                text("UPDATE template_change_set SET rationale = :why WHERE id = :id"),
                {"why": "A different reason, written after the fact.", "id": created.json()["id"]},
            )


def test_approvers_never_apply_continue_or_roll_back(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])
    approver_id = body["decided_by"]["user_id"]
    approver = as_admin(migrated_engine, approver_id, gitlab)
    dee = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, dee)
    path = f"{SETS}/{body['id']}"

    assert "Whoever approved" in refused(approver.post(f"{path}/apply", json={"stage_targets": 1}))
    assert dee.post(f"{path}/apply", json={"stage_targets": 1}).status_code == 200
    run(migrated_engine, gitlab)
    assert "Whoever approved" in refused(approver.post(f"{path}/continue", json={}))
    assert dee.post(f"{path}/continue", json={}).status_code == 200
    run(migrated_engine, gitlab)
    assert "Whoever approved" in refused(approver.post(f"{path}/rollback", json={}))
    eve = as_admin(migrated_engine, user(migrated_engine, "eve"), gitlab)
    assert eve.post(f"{path}/rollback", json={}).status_code == 200
    run(migrated_engine, gitlab, ROLLBACK_CHANGE_SET)

    done = dee.get(path).json()
    assert [(r["kind"], r["user"]["display_name"]) for r in done["runs"]] == [
        ("apply", "Dee"),
        ("continue", "Dee"),
        ("rollback", "Eve"),
    ]
    assert [r["stage_targets"] for r in done["runs"]] == [1, 2, None]
    assert not any(r["development_execution"] for r in done["runs"])

    # The database refuses a run by an approver, and any change to a recorded run.
    with Session(migrated_engine) as db:
        row = db.get_one(TemplateChangeSet, body["id"])
        db.add(
            TemplateChangeRun(
                workspace_id=row.workspace_id,
                change_set_id=row.id,
                kind="rollback",
                user_id=approver_id,
            )
        )
        with pytest.raises(DBAPIError, match="cannot apply, continue or roll it back"):
            db.flush()
    for statement in (
        "UPDATE template_change_run SET user_id = user_id",
        "DELETE FROM template_change_run",
    ):
        with pytest.raises(DBAPIError, match="cannot be changed or deleted"):
            with migrated_engine.begin() as connection:
                connection.execute(text(statement))


def test_development_mode_lets_an_approver_run_it_and_records_that(
    migrated_engine: Engine, world: int, gitlab: GitLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])
    approver = as_admin(migrated_engine, body["decided_by"]["user_id"], gitlab)
    writes_on(monkeypatch, approver)
    monkeypatch.setenv("BALUARTE_DEVELOPMENT_MODE", "true")
    path = f"{SETS}/{body['id']}"

    assert approver.post(f"{path}/apply", json={"stage_targets": 2}).status_code == 200

    (only,) = approver.get(path).json()["runs"]
    assert only["development_execution"] is True
    with Session(migrated_engine) as db:
        started = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "template_change_set.apply_started")
        ).one()
    assert started.details["development_execution"] is True


def test_a_set_links_its_settings_to_controls(migrated_engine: Engine, world: int) -> None:
    body = approved(migrated_engine, RAISE, [API, SITE])

    controls = {(c["framework"], c["control_id"]): c["settings"] for c in body["controls"]}
    assert controls
    assert all(settings == ["merge_requests.min_approvals"] for settings in controls.values())
    assert body["template_versions"] == []


# --- acceptance: the bulk governance workflow end to end ---


def csv_rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(archive.read(name).decode())))


def test_the_bulk_workflow_end_to_end_keeps_a_failure_and_an_exception_visible(
    migrated_engine: Engine,
    world: int,
    gitlab: GitLab,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ana, bea, cy, dee = (user(migrated_engine, name) for name in ("ana", "bea", "cy", "dee"))
    analyst = client(migrated_engine, ana, Role.ANALYST)

    # The site may stay public: an approved template exception.
    asked = analyst.post(
        "/api/v1/template-exceptions",
        json={
            "project_id": project_id(migrated_engine, 101),
            "setting": "visibility",
            "justification": "A public documentation site.",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
    )
    assert asked.status_code == 201, asked.text
    waived = client(migrated_engine, bea, Role.ADMIN).post(
        f"/api/v1/template-exceptions/{asked.json()['id']}/approve", json={}
    )
    assert waived.status_code == 200, waived.text

    # Fix the drift: each project's own effective settings.
    created = analyst.post(
        SETS,
        json={
            "title": "Fix template drift",
            "rationale": WHY,
            "selection": {"projects": [API, SITE]},
            "effective": True,
        },
    )
    assert created.status_code == 201, created.text
    proposal = created.json()
    (site,) = [t for t in proposal["document"]["targets"] if t["project"]["path"] == SITE]
    assert {(s["key"], s["code"]) for s in site["skipped"]} >= {("visibility", "excepted")}
    for approver in (bea, cy)[: proposal["approvals_needed"]]:
        decided = client(migrated_engine, approver, Role.ADMIN).post(
            f"{SETS}/{proposal['id']}/approve", json={"digest": proposal["digest"]}
        )
        assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "approved"

    # A third person applies it; GitLab refuses one write.
    runner = as_admin(migrated_engine, dee, gitlab)
    writes_on(monkeypatch, runner)
    gitlab.failures[("POST", "/projects/101/approvals")] = 403
    path = f"{SETS}/{proposal['id']}"
    applied = runner.post(
        f"{path}/apply", json={"stage_targets": 2, "acknowledge_irreversible": True}
    )
    assert applied.status_code == 200, applied.text
    run(migrated_engine, gitlab)

    done = runner.get(path).json()
    assert done["state"] == "finished"
    outcomes = {(i["project_path"], i["setting"]): i["status"] for i in done["items"]}
    assert outcomes[(SITE, "merge_requests.forbid_author_approval")] == "failed"
    assert "applied" in outcomes.values()
    assert done["rationale"] == WHY
    assert [(r["kind"], r["user"]["user_id"]) for r in done["runs"]] == [("apply", dee)]
    assert done["template_versions"] == ["service@1"]
    assert done["controls"]
    # The failure stays visible in drift, and so does the exception.
    left = {s["key"]: s["status"] for s in drift(migrated_engine, 101)["settings"]}
    assert left["visibility"] == "excepted"

    # The audit-period package holds the record, the failure and the exception.
    evaluate_all(migrated_engine, GITLAB_SETTINGS)
    now = datetime.now(UTC)
    response = client(migrated_engine, ana, Role.AUDITOR).post(
        "/api/v1/evidence/packages",
        json={
            "from": (now - timedelta(days=1)).isoformat(),
            "to": now.isoformat(),
            "sample": {"size": 1, "seed": 1},
        },
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 200, response.text
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    (record,) = csv_rows(archive, "change_sets.csv")
    assert record["id"] == str(proposal["id"])
    assert record["rationale"] == WHY
    assert record["requested_by"] == f"user:{ana}"
    assert record["decided_by"] in (f"user:{bea}", f"user:{cy}")
    assert [r["by"] for r in json.loads(record["runs"])] == [f"user:{dee}"]
    assert record["template_versions"] == "service@1"
    assert record["controls"]
    items = csv_rows(archive, "change_set_items.csv")
    by_key = {(i["project_path"], i["setting"], i["kind"]): i for i in items}
    assert by_key[(SITE, "merge_requests.forbid_author_approval", "change")]["status"] == "failed"
    excepted = by_key[(SITE, "visibility", "skipped")]
    assert excepted["reason"].startswith("excepted:")

    stored = tmp_path / "package.zip"
    stored.write_bytes(response.content)
    manifest = json.loads(archive.read("manifest.json"))
    report, _ = verify_tool.verify(str(stored), details_digest(manifest))
    assert report.failures == []


def test_the_verifier_flags_a_run_by_an_approver() -> None:
    report = verify_tool.Report()
    sets = (
        "id,first_approved_by,decided_by,runs\n"
        '1,,user:2,"[{""kind"": ""apply"", ""by"": ""user:2"", '
        '""development_execution"": false}]"\n'
    )
    verify_tool.verify_change_sets(
        {"change_sets.csv": sets.encode(), "change_set_items.csv": b"change_set_id\n9\n"}, report
    )
    assert report.failures == [
        "change_set_items.csv: item of change set 9 without its set",
        "change set 1: a apply run was started by an approver",
    ]
