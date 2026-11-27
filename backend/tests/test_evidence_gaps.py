"""Evidence gaps (roadmap M15.4): missing, stale, inaccessible and incomplete evidence per control
for a framework version and period."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_assessments import ASSESSMENTS, NARRATIVE, start
from test_evidence_items import accepted, analyst, auditor

pytestmark = pytest.mark.db

GAPS = "/api/v1/evidence-gaps"
PERIOD = {"period_start": "2026-07-01", "period_end": "2026-09-30"}


def gaps(api: TestClient, **params: Any) -> dict[str, Any]:
    response = api.get(GAPS, params={"framework": "soc2", **PERIOD, **params})
    assert response.status_code == 200, response.text
    return dict(response.json())


def control(found: dict[str, Any], control_id: str) -> dict[str, Any]:
    return next(c for c in found["controls"] if c["control_id"] == control_id)


def approve_citing(engine: Engine, citations: list[dict[str, Any]], **body: Any) -> int:
    api = analyst(engine)
    assessment = start(api, **body)
    for citation in citations:
        response = api.post(f"{ASSESSMENTS}/{assessment['id']}/citations", json=citation)
        assert response.status_code == 201, response.text
    assert api.post(f"{ASSESSMENTS}/{assessment['id']}/submit").status_code == 200
    response = auditor(engine).post(f"{ASSESSMENTS}/{assessment['id']}/approve", json={})
    assert response.status_code == 200, response.text
    return int(assessment["id"])


def test_with_no_evidence_every_control_is_missing_and_incomplete(
    migrated_engine: Engine,
) -> None:
    found = gaps(analyst(migrated_engine))
    assert found["version"] == "2017-r2022"
    assert found["evaluation_run_id"] is None
    assert found["controls"]
    for entry in found["controls"]:
        if entry["status"] == "not_applicable":
            assert entry["gaps"] == []
        else:
            assert entry["gaps"] == ["missing", "incomplete"]
            assert len(entry["reasons"]) == 2
    assert found["summary"]["missing"] == found["summary"]["incomplete"] > 0
    assert found["summary"]["stale"] == found["summary"]["inaccessible"] == 0


def test_accepted_items_count_while_fresh(migrated_engine: Engine) -> None:
    accepted(migrated_engine, valid_until="2026-08-01")
    entry = control(gaps(analyst(migrated_engine)), "CC8.1")
    assert entry["accepted_items"] == 1
    assert entry["fresh_items"] == 0
    assert entry["gaps"] == ["stale", "incomplete"]

    accepted(migrated_engine, valid_until="2027-01-01")
    entry = control(gaps(analyst(migrated_engine)), "CC8.1")
    assert entry["accepted_items"] == 2
    assert entry["fresh_items"] == 1
    assert entry["gaps"] == ["incomplete"]
    # An item whose own period ended before this one does not count for it.
    accepted(migrated_engine, period_start="2025-01-01", period_end="2025-03-31")
    assert control(gaps(analyst(migrated_engine)), "CC8.1")["accepted_items"] == 2


def test_an_approved_assessment_closes_incomplete_and_a_failed_link_is_inaccessible(
    migrated_engine: Engine,
) -> None:
    link = accepted(
        migrated_engine, kind="reference", url="https://wiki.example.test/cab", valid_until=None
    )
    assessment_id = approve_citing(
        migrated_engine,
        [{"kind": "evidence_item", "target_id": link["id"], "rationale": "The CAB page."}],
    )
    entry = control(gaps(analyst(migrated_engine)), "CC8.1")
    assert entry["gaps"] == []
    assert entry["assessment_id"] == assessment_id
    assert entry["conclusion"] == "effective"

    # A decided item may still record a check; the failed one makes the citation inaccessible.
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE evidence_item SET check_status = 'unreachable', checked_at = now() "
                "WHERE id = :id"
            ),
            {"id": link["id"]},
        )
    entry = control(gaps(analyst(migrated_engine)), "CC8.1")
    assert entry["gaps"] == ["inaccessible"]

    # Another period has no assessment.
    other = control(
        gaps(analyst(migrated_engine), period_start="2025-01-01", period_end="2025-03-31"),
        "CC8.1",
    )
    assert "incomplete" in other["gaps"]
    assert other["assessment_id"] is None


def test_a_draft_without_citations_is_incomplete(migrated_engine: Engine) -> None:
    approve_citing(
        migrated_engine, [{"kind": "control_status", "rationale": "The automated status."}]
    )
    assert control(gaps(analyst(migrated_engine)), "CC8.1")["gaps"] == ["missing"]
    start(analyst(migrated_engine), narrative=NARRATIVE + " Second look.")
    entry = control(gaps(analyst(migrated_engine)), "CC8.1")
    assert entry["gaps"] == ["missing", "incomplete"]
    assert entry["reasons"][-1] == "An assessment in force cites nothing"


def test_bad_periods_and_versions_are_refused(migrated_engine: Engine) -> None:
    api = analyst(migrated_engine)
    backwards = api.get(
        GAPS,
        params={"framework": "soc2", "period_start": "2026-09-30", "period_end": "2026-07-01"},
    )
    assert backwards.status_code == 422
    too_long = api.get(
        GAPS,
        params={"framework": "soc2", "period_start": "2020-01-01", "period_end": "2026-01-01"},
    )
    assert too_long.status_code == 422
    unknown = api.get(GAPS, params={"framework": "soc2", "version": "1999", **PERIOD})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "version"
    assert gaps(api, framework="nist_800_53_moderate")["version"] == "rev5-moderate"
