"""Control assessments (roadmap M15.3): an assessor's conclusion citing evidence, approved by a
separate reviewer, never changed once decided, superseded by a later one, shown beside the
automated status, and keeping what it cites from retention."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evidence_items import ANA, BEA, CY, accepted, analyst, auditor, client

from baluarte.auth.roles import Role
from baluarte.compliance import retention
from baluarte.db.models import (
    AssessmentCitation,
    AuditEvent,
    ControlAssessment,
    GitLabInstance,
    Observation,
)
from baluarte.workspaces import default_workspace_id

pytestmark = pytest.mark.db

ASSESSMENTS = "/api/v1/assessments"
NARRATIVE = "Sampled 25 changes from the quarter; each had an approved merge request."
CC81 = {"framework": "soc2", "control_id": "CC8.1"}


def start(api: TestClient, **body: Any) -> dict[str, Any]:
    response = api.post(
        ASSESSMENTS,
        json={
            **CC81,
            "period_start": "2026-07-01",
            "period_end": "2026-09-30",
            "conclusion": "effective",
            "narrative": NARRATIVE,
            **body,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def cite_status(api: TestClient, assessment_id: int) -> dict[str, Any]:
    response = api.post(
        f"{ASSESSMENTS}/{assessment_id}/citations",
        json={"kind": "control_status", "rationale": "The automated status for the quarter."},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def approved(engine: Engine, **body: Any) -> dict[str, Any]:
    api = analyst(engine)
    assessment = start(api, **body)
    cite_status(api, assessment["id"])
    assert api.post(f"{ASSESSMENTS}/{assessment['id']}/submit").status_code == 200
    response = auditor(engine).post(f"{ASSESSMENTS}/{assessment['id']}/approve", json={})
    assert response.status_code == 200, response.text
    return dict(response.json())


def observations(engine: Engine, count: int) -> list[int]:
    """Observations of one target, oldest first, collected long ago."""
    with Session(engine) as db:
        instance = GitLabInstance(
            base_url="https://gitlab.example.internal", workspace_id=default_workspace_id(db)
        )
        db.add(instance)
        db.flush()
        payload = {"visibility": "private"}
        rows = [
            Observation(
                instance_id=instance.id,
                source="GET /api/v4/projects/1",
                collected_at=datetime(2001, 1, 1 + n, tzinfo=UTC),
                collection_status="ok",
                payload=payload,
                payload_digest=hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
            )
            for n in range(count)
        ]
        db.add_all(rows)
        db.commit()
        return [row.id for row in rows]


def test_an_assessment_cites_evidence_and_is_approved_by_someone_else(
    migrated_engine: Engine,
) -> None:
    api = analyst(migrated_engine)
    item = accepted(migrated_engine, valid_until="2099-01-01")
    assessment = start(api)
    assert assessment["state"] == "draft"
    assert assessment["control"]["version"] == "2017-r2022"

    submit = f"{ASSESSMENTS}/{assessment['id']}/submit"
    assert api.post(submit).json()["detail"]["code"] == "no_evidence"
    cited = api.post(
        f"{ASSESSMENTS}/{assessment['id']}/citations",
        json={
            "kind": "evidence_item",
            "target_id": item["id"],
            "rationale": "The minutes record each change's approval.",
        },
    ).json()
    [citation] = cited["citations"]
    assert citation["digest"] == item["sha256"]
    assert citation["title"] == "Change board minutes"
    with_status = cite_status(api, assessment["id"])
    snapshot = with_status["citations"][1]["snapshot"]
    assert snapshot["control_id"] == "CC8.1"
    assert with_status["citations"][1]["digest"]

    assert api.post(submit).json()["state"] == "submitted"
    # Nothing changes once submitted, and the assessor cannot review it.
    assert api.put(
        f"{ASSESSMENTS}/{assessment['id']}",
        json={
            "period_start": "2026-07-01",
            "period_end": "2026-09-30",
            "conclusion": "not_effective",
            "narrative": NARRATIVE,
        },
    ).status_code in (403, 409)
    assert cite_status_code(api, assessment["id"]) == 409
    assert analyst(migrated_engine).post(
        f"{ASSESSMENTS}/{assessment['id']}/approve", json={}
    ).status_code in (403, 404)
    own = client(migrated_engine, ANA, Role.ADMIN)
    assert own.post(f"{ASSESSMENTS}/{assessment['id']}/approve", json={}).status_code == 403

    reviewer = auditor(migrated_engine)
    assert (
        reviewer.post(f"{ASSESSMENTS}/{assessment['id']}/reject", json={}).json()["detail"]["code"]
        == "note"
    )
    done = reviewer.post(
        f"{ASSESSMENTS}/{assessment['id']}/approve", json={"note": "Sample checked."}
    ).json()
    assert done["state"] == "approved"
    assert done["reviewed_by"]["user_id"] == BEA
    assert done["development_review"] is False

    with Session(migrated_engine) as db:
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_type == "control_assessment")
        ).all()
    assert {
        "control_assessment.created",
        "control_assessment.cited",
        "control_assessment.submitted",
        "control_assessment.approved",
    } <= set(actions)


def cite_status_code(api: TestClient, assessment_id: int) -> int:
    return api.post(
        f"{ASSESSMENTS}/{assessment_id}/citations",
        json={"kind": "control_status", "rationale": "Late."},
    ).status_code


def test_only_accepted_evidence_items_are_cited(migrated_engine: Engine) -> None:
    api = analyst(migrated_engine)
    draft_item = api.post(
        "/api/v1/evidence-items",
        json={"kind": "reference", "title": "Wiki page", "url": "https://wiki.example.test/a"},
    ).json()
    assessment = start(api)
    refused = api.post(
        f"{ASSESSMENTS}/{assessment['id']}/citations",
        json={"kind": "evidence_item", "target_id": draft_item["id"], "rationale": "Why"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "not_accepted"
    missing = api.post(
        f"{ASSESSMENTS}/{assessment['id']}/citations",
        json={"kind": "observation", "rationale": "Why"},
    )
    assert missing.status_code == 422


def test_development_mode_lets_the_assessor_review_their_own(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALUARTE_DEVELOPMENT_MODE", "true")
    api = client(migrated_engine, CY, Role.ADMIN)
    assessment = start(api)
    cite_status(api, assessment["id"])
    api.post(f"{ASSESSMENTS}/{assessment['id']}/submit")
    done = api.post(f"{ASSESSMENTS}/{assessment['id']}/approve", json={}).json()
    assert done["state"] == "approved"
    assert done["development_review"] is True


def test_the_database_keeps_decided_assessments_and_their_citations_fixed(
    migrated_engine: Engine,
) -> None:
    done = approved(migrated_engine)
    with migrated_engine.connect() as connection:
        for statement in (
            "UPDATE control_assessment SET narrative = 'changed after approval' WHERE id = :id",
            "UPDATE control_assessment SET state = 'submitted' WHERE id = :id",
            "DELETE FROM control_assessment WHERE id = :id",
            "DELETE FROM assessment_citation WHERE assessment_id = :id",
            "INSERT INTO assessment_citation (assessment_id, kind, snapshot, rationale) "
            "VALUES (:id, 'control_status', '{}', 'late')",
        ):
            with pytest.raises(DBAPIError, match=r"restrict_violation|cannot|only|cited"):
                with connection.begin():
                    connection.execute(text(statement), {"id": done["id"]})


def test_a_draft_is_edited_uncited_and_deleted_only_by_its_assessor(
    migrated_engine: Engine,
) -> None:
    api = analyst(migrated_engine)
    assessment = start(api)
    cited = cite_status(api, assessment["id"])
    other = analyst(migrated_engine, user_id=CY)
    path = f"{ASSESSMENTS}/{assessment['id']}"
    assert other.delete(path).status_code == 403
    citation_id = cited["citations"][0]["id"]
    assert other.delete(f"{path}/citations/{citation_id}").status_code == 403
    assert api.delete(f"{path}/citations/{citation_id}").json()["citations"] == []
    edited = api.put(
        path,
        json={
            "period_start": "2026-07-01",
            "period_end": "2026-09-30",
            "conclusion": "partially_effective",
            "narrative": NARRATIVE + " Two lacked a second approver.",
        },
    )
    assert edited.json()["conclusion"] == "partially_effective"
    bad = api.put(
        path,
        json={
            "period_start": "2026-09-30",
            "period_end": "2026-07-01",
            "conclusion": "effective",
            "narrative": NARRATIVE,
        },
    )
    assert bad.status_code == 422
    assert api.delete(path).status_code == 204
    assert api.get(path).status_code == 404


def test_a_later_assessment_supersedes_an_approved_one(migrated_engine: Engine) -> None:
    first = approved(migrated_engine)
    api = analyst(migrated_engine)
    other_control = api.post(
        ASSESSMENTS,
        json={
            "framework": "soc2",
            "control_id": "CC6.1",
            "period_start": "2026-07-01",
            "period_end": "2026-09-30",
            "conclusion": "effective",
            "narrative": NARRATIVE,
            "supersedes_id": first["id"],
        },
    )
    assert other_control.status_code == 409
    second = approved(migrated_engine, conclusion="not_effective", supersedes_id=first["id"])
    assert second["supersedes_id"] == first["id"]
    reread = api.get(f"{ASSESSMENTS}/{first['id']}").json()
    assert reread["superseded_by_id"] == second["id"]
    again = api.post(
        ASSESSMENTS,
        json={
            **CC81,
            "period_start": "2026-07-01",
            "period_end": "2026-09-30",
            "conclusion": "effective",
            "narrative": NARRATIVE,
            "supersedes_id": first["id"],
        },
    )
    assert again.status_code == 409

    listed = api.get(ASSESSMENTS, params={"control": "soc2:CC8.1", "state": "approved"}).json()
    assert [a["id"] for a in listed["items"]] == [second["id"], first["id"]]
    detail = api.get("/api/v1/controls/soc2/CC8.1").json()
    assert detail["assessment"]["id"] == second["id"]
    assert detail["assessment"]["conclusion"] == "not_effective"
    assert detail["assessment"]["framework_version"] == "2017-r2022"
    assert isinstance(detail["assessment"]["disagrees"], bool)
    assert api.get("/api/v1/controls/soc2/CC6.1").json()["assessment"] is None


def test_retention_keeps_what_an_assessment_cites(migrated_engine: Engine) -> None:
    item = accepted(migrated_engine, valid_until="2001-01-01")
    # The newer observation of the same target means retention would otherwise remove the old.
    observation_id, newer = observations(migrated_engine, 2)
    api = analyst(migrated_engine)
    assessment = start(api)
    for kind, target in (("evidence_item", item["id"]), ("observation", observation_id)):
        response = api.post(
            f"{ASSESSMENTS}/{assessment['id']}/citations",
            json={"kind": kind, "target_id": target, "rationale": "Cited."},
        )
        assert response.status_code == 201, response.text
    with Session(migrated_engine) as db:
        details = retention.expire(db)
        db.commit()
        assert details["deleted"]["evidence_item_file"] == 0
        assert db.get(Observation, observation_id) is not None
        assert db.get(Observation, newer) is not None

    # Once the draft is gone, nothing holds the file back.
    assert api.delete(f"{ASSESSMENTS}/{assessment['id']}").status_code == 204
    with Session(migrated_engine) as db:
        assert db.scalar(select(AssessmentCitation.id)) is None
        assert db.scalar(select(ControlAssessment.id)) is None
        details = retention.expire(db)
        db.commit()
    assert details["deleted"]["evidence_item_file"] == 1
