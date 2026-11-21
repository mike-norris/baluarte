"""Issue-to-control mappings and audit periods (roadmap M12.3)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import FakeGitLab
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_control_mapping import client, require_review
from test_issue_evidence import capture, evidence_routes, full_issue
from test_issue_evidence import indexed as indexed  # the fixture
from test_issue_register import collect, serve
from test_issue_register import world as world  # the fixture

from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent

pytestmark = pytest.mark.db

MAPPINGS = "/api/v1/register/mappings"
WHY = "The closing merge request shows the change was reviewed and approved."
ITEM_WHY = "Shows a second person approved it."


@pytest.fixture
def captured(migrated_engine: Engine, world: dict[str, int], indexed: FakeGitLab) -> FakeGitLab:
    evidence_routes(indexed, 1)
    capture(migrated_engine, world, indexed)
    return indexed


def analyst(engine: Engine, user_id: int = 1) -> TestClient:
    return client(engine, user_id, Role.ANALYST)


def propose(
    engine: Engine,
    project: int,
    keys: list[str],
    user_id: int = 1,
    iid: int = 1,
    control_id: str = "CC8.1",
    **extra: Any,
) -> Any:
    return analyst(engine, user_id).post(
        MAPPINGS,
        json={
            "project_id": project,
            "iid": iid,
            "framework": "soc2",
            "control_id": control_id,
            "rationale": WHY,
            "items": [{"key": key, "rationale": ITEM_WHY} for key in keys],
            **extra,
        },
    )


def gaps(mapping: dict[str, Any]) -> list[str]:
    return [gap["code"] for gap in mapping["gaps"]]


def test_a_mapping_cites_captured_items_and_is_decided_by_someone_else(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab
) -> None:
    require_review(migrated_engine)
    project = world["project"]
    citable = analyst(migrated_engine).get(f"/api/v1/register/issues/{project}/1/mappings").json()
    keys = {item["key"] for item in citable["citable"]}
    assert {"description", "note:11", "event:state:1", "merge_request:42:5", "pipeline:99"} <= keys
    assert any(key.startswith("attachment:") for key in keys)

    proposed = propose(migrated_engine, project, ["merge_request:42:5", "note:11"])
    assert proposed.status_code == 201, proposed.text
    mapping = proposed.json()
    assert (mapping["state"], mapping["self_approved"]) == ("proposed", False)
    assert mapping["control_title"].startswith("Changes are authorized")
    assert [e["key"] for e in mapping["evidence"]] == ["merge_request:42:5", "note:11"]
    assert mapping["evidence"][0]["label"].startswith("Merge request !5 (merged, approved by")
    assert gaps(mapping) == ["awaiting_review"]

    own = analyst(migrated_engine).post(f"{MAPPINGS}/{mapping['id']}/approve", json={})
    assert own.status_code == 403
    queue = analyst(migrated_engine, 2).get("/api/v1/reviews").json()["items"]
    (item,) = [i for i in queue if i["kind"] == "issue_mapping"]
    assert (item["project_id"], item["iid"], item["can_decide"]) == (project, 1, True)
    assert item["title"] == "acme/api#1 for SOC2 CC8.1"

    approved = analyst(migrated_engine, 2).post(
        f"{MAPPINGS}/{mapping['id']}/approve", json={"note": "Agreed."}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "active"
    assert gaps(approved.json()) == []
    with Session(migrated_engine) as db:
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_type == "issue_control_mapping")
        ).all()
    assert actions == ["register.mapping_proposed", "register.mapping_approved"]


def test_without_review_a_mapping_is_in_force_and_revised_not_edited(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab
) -> None:
    project = world["project"]
    first = propose(migrated_engine, project, ["merge_request:42:5"]).json()
    assert (first["state"], first["self_approved"]) == ("active", True)

    duplicate = propose(migrated_engine, project, ["note:11"])
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate"

    revision = propose(migrated_engine, project, ["note:11"], supersedes_id=first["id"])
    assert revision.status_code == 201, revision.text
    listed = analyst(migrated_engine).get(f"/api/v1/register/issues/{project}/1/mappings").json()
    states = {m["id"]: m["state"] for m in listed["items"]}
    assert states == {first["id"]: "withdrawn", revision.json()["id"]: "active"}
    old = next(m for m in listed["items"] if m["id"] == first["id"])
    assert old["withdraw_reason"] == f"Revised by mapping {revision.json()['id']}."

    withdrawn = analyst(migrated_engine, 2).post(
        f"{MAPPINGS}/{revision.json()['id']}/withdraw",
        json={"reason": "The change was rolled back in GitLab."},
    )
    assert withdrawn.status_code == 200
    again = analyst(migrated_engine, 2).post(
        f"{MAPPINGS}/{revision.json()['id']}/withdraw",
        json={"reason": "The change was rolled back in GitLab."},
    )
    assert again.status_code == 409


def test_a_mapping_must_name_a_control_an_auditable_issue_and_captured_items(
    migrated_engine: Engine, world: dict[str, int], indexed: FakeGitLab
) -> None:
    project = world["project"]
    not_captured = propose(migrated_engine, project, ["note:11"])
    assert not_captured.status_code == 409
    assert "not_captured" in [r["code"] for r in not_captured.json()["detail"]["reasons"]]

    evidence_routes(indexed, 1)
    capture(migrated_engine, world, indexed)
    unknown_control = propose(migrated_engine, project, ["note:11"], control_id="ZZ9.9")
    unknown_item = propose(migrated_engine, project, ["note:999"])
    repeated = propose(migrated_engine, project, ["note:11", "note:11"])
    not_auditable = propose(migrated_engine, project, ["note:11"], iid=2)

    assert unknown_control.json()["detail"]["code"] == "control_unknown"
    assert unknown_item.json()["detail"]["code"] == "item_unknown"
    assert repeated.json()["detail"]["code"] == "item_repeated"
    reasons = [r["code"] for r in not_auditable.json()["detail"]["reasons"]]
    assert reasons[0] == "issue_not_auditable"
    auditor = client(migrated_engine, 7, Role.AUDITOR)
    assert auditor.post(f"{MAPPINGS}/1/approve", json={}).status_code == 403


def test_a_period_keeps_evidence_apart_from_later_remediation(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab
) -> None:
    project = world["project"]
    writer = analyst(migrated_engine)
    period = writer.post(
        "/api/v1/register/periods",
        json={"name": "SOC 2 2026", "starts_on": "2026-10-01", "ends_on": "2026-11-01"},
    )
    assert period.status_code == 201, period.text
    period_id = period.json()["id"]
    # The label was added on 1 November, inside; the comment came a day later, after.
    inside = propose(migrated_engine, project, ["event:label:2"]).json()
    later = propose(migrated_engine, project, ["note:11"], control_id="CC6.1").json()

    listed = writer.get(
        f"/api/v1/register/issues/{project}/1/mappings?period_id={period_id}"
    ).json()
    by_id = {m["id"]: m for m in listed["items"]}
    assert by_id[inside["id"]]["evidence"][0]["timing"] == "in_period"
    assert gaps(by_id[inside["id"]]) == []
    assert by_id[later["id"]]["evidence"][0]["timing"] == "after"
    assert gaps(by_id[later["id"]]) == ["only_later_remediation"]

    coverage = writer.get(f"/api/v1/register/coverage?period_id={period_id}").json()["rows"]
    assert {(r["control_id"], r["issues"], r["in_period"]) for r in coverage} == {
        ("CC8.1", 1, 1),
        ("CC6.1", 1, 0),
    }
    assert next(r for r in coverage if r["control_id"] == "CC6.1")["gaps"] == {
        "only_later_remediation": 1
    }

    bad = writer.post(
        "/api/v1/register/periods",
        json={"name": "Backwards", "starts_on": "2026-11-01", "ends_on": "2026-10-01"},
    )
    assert bad.status_code == 422
    ended = writer.post(f"/api/v1/register/periods/{period_id}/end")
    assert ended.json()["ended_by"] == "user:1"
    assert writer.post(f"/api/v1/register/periods/{period_id}/end").status_code == 409


def test_closing_alone_is_never_proof_and_a_newer_capture_is_flagged(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab
) -> None:
    project = world["project"]
    closure = propose(migrated_engine, project, ["event:state:1"]).json()
    assert gaps(closure) == ["closure_only"]

    # GitLab changes the issue and its evidence is captured again.
    serve(captured, full_issue(1, updated_at="2026-11-20T10:00:00Z"))
    collect(migrated_engine, world, captured)
    evidence_routes(
        captured,
        1,
        **{
            "/projects/42/issues/1/notes": [
                {"id": 11, "author": {"username": "ada"}, "body": "Edited.", "system": False}
            ]
        },
    )
    capture(migrated_engine, world, captured)

    listed = analyst(migrated_engine).get(f"/api/v1/register/issues/{project}/1/mappings").json()
    assert gaps(listed["items"][0]) == ["recaptured", "closure_only"]
    # The mapping still reads as it was made.
    assert listed["items"][0]["evidence"][0]["label"] == "grace closed the issue"


def test_mappings_cited_evidence_and_periods_cannot_be_rewritten(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab
) -> None:
    mapping = propose(migrated_engine, world["project"], ["note:11"]).json()
    analyst(migrated_engine).post(
        "/api/v1/register/periods",
        json={"name": "FY26", "starts_on": "2026-01-01", "ends_on": "2026-12-31"},
    )
    statements = [
        "UPDATE issue_control_mapping SET rationale = 'x' WHERE id = :id",
        "DELETE FROM issue_control_mapping WHERE id = :id",
        "UPDATE issue_control_evidence SET rationale = 'something else entirely'",
        "DELETE FROM issue_control_evidence",
        "UPDATE audit_period SET ends_on = '2027-12-31'",
        "DELETE FROM audit_period",
    ]
    for statement in statements:
        with Session(migrated_engine) as db, pytest.raises(DBAPIError):
            db.execute(text(statement), {"id": mapping["id"]} if ":id" in statement else {})
