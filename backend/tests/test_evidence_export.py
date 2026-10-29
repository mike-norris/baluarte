"""Scoped evidence export: scope, permissions, content, CSV safety, secrets and auditing."""

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from test_evaluation_store import VISIBILITY_ONLY, World
from test_findings_api import client_as

from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, GitLabGroup, GitLabProject
from baluarte.evidence import export
from baluarte.evidence.export import cell
from baluarte.evidence.observations import Observed, record

pytestmark = pytest.mark.db

ENDPOINT = "/api/v1/evidence/exports"
# Assembled at runtime so the source holds no token-shaped literal.
FAKE_PAT = "gl" + "pat-" + "Zq7" * 7
SECTIONS = [
    "inventory",
    "evaluations",
    "findings",
    "observations",
    "policies",
    "collection_errors",
    "collection_runs",
]


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


@pytest.fixture
def world(migrated_engine: Engine) -> World:
    world = World(migrated_engine)
    world.assign()
    return world


def post(engine: Engine, body: dict[str, Any], role: Role | None = Role.AUDITOR) -> httpx.Response:
    roles = () if role is None else (role,)
    return client_as(engine, *roles).post(ENDPOINT, json=body)


def export_json(engine: Engine, **scope: Any) -> dict[str, Any]:
    response = post(engine, {"format": "json", "scope": scope or {"all": True}})
    assert response.status_code == 200, response.text
    document: dict[str, Any] = response.json()
    return document


def export_zip(engine: Engine, **scope: Any) -> tuple[httpx.Response, zipfile.ZipFile]:
    response = post(engine, {"format": "csv", "scope": scope or {"all": True}})
    assert response.status_code == 200, response.text
    return response, zipfile.ZipFile(io.BytesIO(response.content))


def rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(archive.read(name).decode())))


def other_project(world: World, gitlab_id: int, visibility: str) -> int:
    """A project in a group outside ``platform``."""
    with Session(world.engine) as db:
        if db.scalars(select(GitLabGroup).where(GitLabGroup.gitlab_id == 20)).first() is None:
            db.add(
                GitLabGroup(
                    instance_id=world.instance_id, gitlab_id=20, full_path="other", name="other"
                )
            )
        row = GitLabProject(
            instance_id=world.instance_id,
            gitlab_id=gitlab_id,
            namespace_gitlab_id=20,
            namespace_kind="group",
            path_with_namespace=f"other/x{gitlab_id}",
            name=f"x{gitlab_id}",
            visibility=visibility,
        )
        db.add(row)
        db.commit()
        project_id = row.id
    world.observe(project_id, visibility)
    return project_id


def audit_events(engine: Engine) -> list[AuditEvent]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_type == "evidence_export")
                .order_by(AuditEvent.id)
            )
        )


def by_project(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {item["project_gitlab_id"]: item for item in items}


def test_a_json_export_holds_every_section_with_provenance(world: World) -> None:
    world.project(1, "public")
    world.project(2, "private")
    world.project(3, None, observe=False)
    world.run()

    response = post(world.engine, {"format": "json", "scope": {"all": True}})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="baluarte-evidence-')
    assert disposition.endswith('.json"')
    document = response.json()
    assert list(document) == [
        "format",
        "export_id",
        "generated_at",
        "instance",
        "scope",
        "freshness",
        *SECTIONS,
        "counts",
        "manifest",
    ]
    assert document["format"] == "baluarte.evidence-export/v1"
    assert document["export_id"] == response.headers["x-baluarte-export-id"]
    assert document["scope"] == {"all": True}
    assert document["freshness"]["collection"]["status"] in {"fresh", "stale", "never", "failed"}
    assert document["freshness"]["evaluation"]["status"] == "fresh"
    assert [p["gitlab_id"] for p in document["inventory"]] == [1, 2, 3]

    evaluations = by_project(document["evaluations"])
    assert {k: v["outcome"] for k, v in evaluations.items()} == {
        1: "fail",
        2: "pass",
        3: "unknown",
    }
    assert evaluations[3]["unknown_reasons"]
    assert evaluations[3]["observed"] is None
    assert evaluations[1]["policy_version"] == 1
    assert evaluations[1]["expected"] == {"allowed": ["private", "internal"]}

    (finding,) = document["findings"]
    assert (finding["project_gitlab_id"], finding["state"]) == (1, "open")
    assert finding["latest_observation_id"] == evaluations[1]["observation_id"]

    observations = {o["id"]: o for o in document["observations"]}
    assert set(observations) == {
        e["observation_id"] for e in document["evaluations"] if e["observation_id"]
    }
    for observation in observations.values():
        assert observation["digest_valid"] is True
        assert observation["payload_withheld"] is False
        assert observation["collection_status"] == "ok"
        assert observation["source"] == "GET /projects"
        canonical = json.dumps(observation["payload"], sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(canonical.encode()).hexdigest() == observation["payload_digest"]

    (policy,) = document["policies"]
    assert policy["name"] == "default-gitlab-baseline"
    assert policy["source_yaml"] == VISIBILITY_ONLY
    assert policy["source_yaml_redacted"] is False
    assert hashlib.sha256(VISIBILITY_ONLY.encode()).hexdigest() == policy["yaml_digest"]

    runs = {(r["run_type"], r["kind"]) for r in document["collection_runs"]}
    assert runs == {("collection", "projects"), ("evaluation", "evaluation")}
    assert document["counts"] == {
        "inventory": 3,
        "evaluations": 3,
        "findings": 1,
        "observations": 2,
        "policies": 1,
        "collection_errors": 0,
        "collection_runs": 2,
    }


def test_unknown_and_failed_collection_state_is_exported_not_dropped(world: World) -> None:
    world.project(1, "private", state="missing")
    world.project(2, "private")
    with Session(world.engine) as db:
        project = db.scalars(select(GitLabProject).where(GitLabProject.gitlab_id == 2)).one()
        project.last_error = "could not confirm: GitLab returned 502 for /projects/102"
        db.commit()
    world.collection("failed")
    world.run()

    document = export_json(world.engine, all=True)

    errors = {(e["resource_type"], e["gitlab_id"]): e for e in document["collection_errors"]}
    assert errors[("project", 1)]["state"] == "missing"
    assert "502" in errors[("project", 2)]["error"]
    assert document["freshness"]["collection"]["collections"]["projects"]["status"] == "failed"
    outcomes = {e["project_gitlab_id"]: e["outcome"] for e in document["evaluations"]}
    assert set(outcomes.values()) <= {"unknown"}


def test_scope_by_group_project_and_rule(world: World) -> None:
    world.project(1, "public")
    world.project(2, "public")
    other_project(world, 30, "public")
    world.run()

    def projects(section: str, **scope: Any) -> list[int]:
        document = export_json(world.engine, **scope)
        key = "gitlab_id" if section == "inventory" else "project_gitlab_id"
        return sorted(item[key] for item in document[section])

    assert projects("inventory", group="platform") == [1, 2]
    assert projects("evaluations", group="platform") == [1, 2]
    assert projects("findings", group="other") == [30]
    assert projects("evaluations", group="OTHER") == [30]
    assert projects("inventory", projects=[2, 30]) == [2, 30]
    assert projects("evaluations", policy="default-gitlab-baseline", rule="project-visibility") == [
        1,
        2,
        30,
    ]
    assert projects("evaluations", rule="no-such-rule") == []
    assert projects("inventory", rule="no-such-rule") == [1, 2, 30]


def test_a_group_scope_matches_whole_path_segments(world: World) -> None:
    world.project(1, "public")
    with Session(world.engine) as db:
        db.add(
            GitLabGroup(instance_id=world.instance_id, gitlab_id=11, full_path="plat", name="plat")
        )
        db.commit()

    document = export_json(world.engine, group="plat")

    assert document["inventory"] == []


def test_scope_by_outcome_and_finding_state(world: World) -> None:
    first = world.project(1, "public")
    world.project(2, "public")
    world.project(3, "private")
    world.run()
    world.observe(first, "private")
    world.run()

    def evaluated(**scope: Any) -> dict[int, str]:
        document = export_json(world.engine, **scope)
        return {e["project_gitlab_id"]: e["outcome"] for e in document["evaluations"]}

    assert evaluated(outcome="fail") == {2: "fail"}
    assert evaluated(outcome="pass") == {1: "pass", 3: "pass"}
    assert evaluated(finding_state="resolved") == {1: "pass"}
    document = export_json(world.engine, finding_state="open")
    assert [f["project_gitlab_id"] for f in document["findings"]] == [2]


def test_without_a_window_only_the_latest_result_is_exported(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    world.observe(project_id, "private")
    world.run()

    latest = export_json(world.engine, projects=[1])
    history = export_json(world.engine, projects=[1], since="2000-01-01T00:00:00Z")
    nothing = export_json(world.engine, projects=[1], until="2000-01-01T00:00:00Z")

    assert [e["outcome"] for e in latest["evaluations"]] == ["pass"]
    assert [e["outcome"] for e in history["evaluations"]] == ["fail", "pass"]
    assert len(history["observations"]) == 2
    assert nothing["evaluations"] == []
    assert nothing["collection_runs"] == []


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"projects": []},
        {"all": True, "group": "platform"},
        {"all": False},
        {"group": "/platform"},
        {"projects": [0]},
        {"outcome": "compliant"},
        {"since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
        {"since": "2026-01-01T00:00:00"},
        {"all": True, "extra": 1},
    ],
)
def test_invalid_scopes_are_rejected(world: World, scope: dict[str, Any]) -> None:
    response = post(world.engine, {"format": "json", "scope": scope})

    assert response.status_code == 422, response.text
    assert audit_events(world.engine) == []


def test_unknown_targets_and_formats_are_rejected(world: World) -> None:
    world.project(1, "private")

    group = post(world.engine, {"format": "json", "scope": {"group": "nope"}})
    projects = post(world.engine, {"format": "json", "scope": {"projects": [1, 999, 998]}})
    fmt = post(world.engine, {"format": "pdf", "scope": {"all": True}})

    assert group.status_code == 422
    assert group.json()["detail"] == "No group with the path 'nope' is in the inventory"
    assert projects.status_code == 422
    assert projects.json()["detail"] == "Projects not in the inventory: 998, 999"
    assert fmt.status_code == 422
    assert audit_events(world.engine) == []


def test_a_scope_over_the_row_limit_is_refused_before_sending(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for gitlab_id in (1, 2):
        world.project(gitlab_id, "public")
    world.run()
    monkeypatch.setattr(export, "MAX_ROWS", 5)

    response = post(world.engine, {"format": "csv", "scope": {"all": True}})

    assert response.status_code == 422
    assert "selects 6 rows; an export holds at most 5" in response.json()["detail"]
    assert audit_events(world.engine) == []
    assert export_json(world.engine, projects=[1])["counts"]["inventory"] == 1


@pytest.mark.parametrize(
    ("role", "expected"),
    [(Role.AUDITOR, 200), (Role.ADMIN, 200), (Role.ANALYST, 403), (None, 403)],
)
def test_exporting_needs_evidence_export(world: World, role: Role | None, expected: int) -> None:
    world.project(1, "private")

    response = post(world.engine, {"format": "json", "scope": {"all": True}}, role=role)

    assert response.status_code == expected
    if expected == 403:
        assert audit_events(world.engine) == []


def test_not_configured_or_nothing_collected(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = post(migrated_engine, {"format": "json", "scope": {"all": True}})
    monkeypatch.delenv("BALUARTE_GITLAB_URL")
    unconfigured = post(migrated_engine, {"format": "json", "scope": {"all": True}})

    assert (empty.status_code, empty.json()["detail"]) == (409, "Nothing has been collected yet")
    assert (unconfigured.status_code, unconfigured.json()["detail"]) == (
        409,
        "GitLab is not configured",
    )


def test_a_csv_export_is_a_zip_of_csv_files(world: World) -> None:
    world.project(1, "public")
    world.project(2, "private")
    world.run()

    response, archive = export_zip(world.engine, all=True)

    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].endswith('.zip"')
    assert archive.namelist() == [
        "README.txt",
        "export.json",
        "inventory.csv",
        "evaluations.csv",
        "findings.csv",
        "observations.jsonl",
        "policies.csv",
        "collection_errors.csv",
        "collection_runs.csv",
        "manifest.json",
        "SHA256SUMS",
    ]
    assert archive.testzip() is None
    readme = archive.read("README.txt").decode()
    assert "not a SOC 2 certification" in readme
    assert response.headers["x-baluarte-export-id"] in readme
    meta = json.loads(archive.read("export.json"))
    assert meta["scope"] == {"all": True}

    evaluations = rows(archive, "evaluations.csv")
    assert list(evaluations[0]) == list(export.COLUMNS["evaluations"])
    assert {(r["project_gitlab_id"], r["outcome"]) for r in evaluations} == {
        ("1", "fail"),
        ("2", "pass"),
    }
    failed = next(r for r in evaluations if r["outcome"] == "fail")
    assert failed["expected"] == '{"allowed":["private","internal"]}'
    assert failed["unknown_reasons"] == "[]"
    observations = [json.loads(line) for line in archive.read("observations.jsonl").splitlines()]
    assert {o["id"] for o in observations} == {int(r["observation_id"]) for r in evaluations}
    assert rows(archive, "collection_errors.csv") == []
    assert rows(archive, "findings.csv")[0]["state"] == "open"


def test_csv_cells_cannot_run_as_formulas(world: World) -> None:
    world.project(1, "private")
    with Session(world.engine) as db:
        project = db.scalars(select(GitLabProject).where(GitLabProject.gitlab_id == 1)).one()
        project.last_error = '=HYPERLINK("https://evil.example","x")'
        db.commit()

    _, archive = export_zip(world.engine, all=True)

    (error,) = rows(archive, "collection_errors.csv")
    assert error["error"] == '\'=HYPERLINK("https://evil.example","x")'
    (project_row,) = rows(archive, "inventory.csv")
    assert project_row["last_error"].startswith("'=")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (True, "true"),
        (-3, "-3"),
        ("=1+1", "'=1+1"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ("plain", "plain"),
        ({"b": 1, "a": "=x"}, '{"a":"=x","b":1}'),
        (["-x"], '["-x"]'),
    ],
)
def test_cell(value: Any, expected: str) -> None:
    assert cell(value) == expected


def test_credentials_never_leave_in_an_export(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    world.assign(VISIBILITY_ONLY + f"# rotated {FAKE_PAT} on Monday\n")
    leaky = world.project(1, "private", observe=False)
    world.project(2, "private")
    with Session(world.engine) as db:
        record(
            db,
            world.instance_id,
            "project_id",
            [
                Observed(
                    leaky,
                    "GET /projects",
                    "/projects/1",
                    "ok",
                    {"gitlab_id": 1, "visibility": "private", "name": FAKE_PAT},
                )
            ],
        )
        project = db.get_one(GitLabProject, leaky)
        project.last_error = f"GitLab said: private_token={FAKE_PAT} and {TOKEN}"
        db.commit()
    world.run()

    as_json = post(world.engine, {"format": "json", "scope": {"all": True}})
    _, archive = export_zip(world.engine, all=True)
    unpacked = b"".join(archive.read(name) for name in archive.namelist())

    for body in (as_json.content, unpacked):
        assert FAKE_PAT.encode() not in body
        assert TOKEN.encode() not in body
    document = as_json.json()
    withheld = next(o for o in document["observations"] if o["project_gitlab_id"] == 1)
    assert withheld["payload_withheld"] is True
    assert withheld["payload"] is None
    assert withheld["digest_valid"] is True
    assert document["policies"][0]["source_yaml_redacted"] is True
    assert "[REDACTED]" in document["policies"][0]["source_yaml"]


def test_tampered_evidence_is_exported_with_digest_valid_false(world: World) -> None:
    project_id = world.project(1, "private")
    world.run()
    with world.engine.begin() as connection:
        # Simulates a privileged change that bypasses the trigger.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text(
                "UPDATE observation SET payload = jsonb_set(payload, '{visibility}', "
                "'\"public\"') WHERE project_id = :project_id"
            ),
            {"project_id": project_id},
        )

    document = export_json(world.engine, projects=[1])

    (observation,) = document["observations"]
    assert observation["digest_valid"] is False
    assert observation["payload"]["visibility"] == "public"


def test_the_same_evidence_gives_the_same_export(world: World) -> None:
    world.project(1, "public")
    world.project(2, "private")
    world.run()

    def stable(manifest: dict[str, Any]) -> dict[str, Any]:
        # The export ID and times differ, and so do the hashes of what holds them.
        for key in ("export_id", "generated_at", "freshness", "metadata_sha256"):
            manifest.pop(key, None)
        manifest["files"] = [
            entry
            for entry in manifest.get("files", [])
            if entry["name"] not in {"README.txt", "export.json"}
        ]
        return manifest

    def normalized() -> dict[str, Any]:
        document = export_json(world.engine, all=True)
        for key in ("export_id", "generated_at", "freshness"):
            document.pop(key)
        document["manifest"] = stable(document["manifest"])
        return document

    first, second = normalized(), normalized()
    _, zip_one = export_zip(world.engine, all=True)
    _, zip_two = export_zip(world.engine, all=True)

    assert first == second
    volatile = {"README.txt", "export.json", "manifest.json", "SHA256SUMS"}
    for name in zip_one.namelist():
        assert zip_one.getinfo(name).date_time == (1980, 1, 1, 0, 0, 0)
        if name not in volatile:
            assert zip_one.read(name) == zip_two.read(name), name
    manifests = [stable(json.loads(z.read("manifest.json"))) for z in (zip_one, zip_two)]
    assert manifests[0] == manifests[1]


def test_every_export_is_audited_without_its_data(world: World) -> None:
    world.project(1, "public")
    world.run()

    response = post(world.engine, {"format": "csv", "scope": {"group": "platform"}})

    assert response.status_code == 200
    accepted, completed = audit_events(world.engine)
    export_id = response.headers["x-baluarte-export-id"]
    assert (accepted.action, accepted.resource_id, accepted.actor) == (
        "evidence.exported",
        export_id,
        "user:1",
    )
    assert accepted.details == {
        "format": "csv",
        "scope": {"group": "platform"},
        "planned_rows": {"inventory": 1, "evaluations": 1, "findings": 1, "collection_errors": 0},
        "instance": URL,
    }
    assert (completed.action, completed.resource_id) == ("evidence.export_completed", export_id)
    assert completed.details["sha256"] == hashlib.sha256(response.content).hexdigest()
    assert completed.details["bytes"] == len(response.content)
    assert completed.details["rows"]["evaluations"] == 1
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    files = completed.details["files"]
    assert set(files) == set(archive.namelist())
    for name, digest in files.items():
        assert digest["sha256"] == hashlib.sha256(archive.read(name)).hexdigest()
    assert files["evaluations.csv"]["rows"] == 1
    assert "platform/p1" not in json.dumps([accepted.details, completed.details])
