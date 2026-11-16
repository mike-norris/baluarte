"""The review queue: what waits for a second person, who may decide it, and who may see it."""

from datetime import timedelta

import pytest
import test_compliance
import test_policy_changes
from sqlalchemy import Engine

from baluarte.auth.roles import Role

pytestmark = pytest.mark.db

gitlab_env = test_policy_changes.gitlab_env
instance_id = test_policy_changes.instance_id
engine = test_compliance.engine
client = test_policy_changes.client


def propose_baseline(engine: Engine, user_id: int) -> int:
    test_policy_changes.require_review(engine)
    saved = client(engine, user_id, Role.ANALYST).post(
        "/api/v1/policies", json={"source_yaml": test_policy_changes.FRESH}
    )
    assert saved.status_code == 202, saved.text
    return int(saved.json()["id"])


def test_an_empty_queue(migrated_engine: Engine) -> None:
    response = client(migrated_engine, 1, Role.AUDITOR).get("/api/v1/reviews")

    assert response.status_code == 200
    assert response.json() == {"items": [], "partial": []}


def test_a_proposal_is_decided_by_someone_else_with_the_permission(
    migrated_engine: Engine, instance_id: int
) -> None:
    del instance_id
    propose_baseline(migrated_engine, 1)

    own = client(migrated_engine, 1, Role.ANALYST).get("/api/v1/reviews").json()
    other = client(migrated_engine, 2, Role.ANALYST).get("/api/v1/reviews").json()
    reader = client(migrated_engine, 3, Role.AUDITOR).get("/api/v1/reviews").json()

    [item] = own["items"]
    assert item["kind"] == "baseline_change"
    assert item["title"] == "fresh"
    assert item["proposed_by"]["user_id"] == 1
    assert item["expires_at"] is None
    assert item["can_decide"] is False
    assert [entry["can_decide"] for entry in other["items"]] == [True]
    assert [entry["can_decide"] for entry in reader["items"]] == [False]


def test_mine_keeps_only_the_callers_proposals(migrated_engine: Engine, instance_id: int) -> None:
    del instance_id
    propose_baseline(migrated_engine, 1)

    mine = client(migrated_engine, 1, Role.ANALYST).get("/api/v1/reviews?mine=true").json()
    theirs = client(migrated_engine, 2, Role.ANALYST).get("/api/v1/reviews?mine=true").json()

    assert [item["proposed_by"]["user_id"] for item in mine["items"]] == [1]
    assert theirs["items"] == []


def test_kinds_the_caller_may_not_read_are_left_out(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)
    assert (
        admin.put("/api/v1/compliance/profile", json=test_compliance.body(["soc2"])).status_code
        == 200
    )
    requested = admin.post(
        "/api/v1/compliance/exceptions",
        json={
            "control_id": "change-review",
            "justification": "One administrator until the second hire starts",
            "expires_on": (test_compliance.utc_today() + timedelta(days=30)).isoformat(),
        },
    )
    assert requested.status_code == 201, requested.text

    def kinds(user_id: int, role: Role) -> list[tuple[str, bool]]:
        items = client(engine, user_id, role).get("/api/v1/reviews").json()["items"]
        return [(item["kind"], item["can_decide"]) for item in items]

    # Analysts may not read compliance exceptions; auditors may, but only admins decide them.
    assert kinds(2, Role.ANALYST) == []
    assert kinds(3, Role.AUDITOR) == [("control_exception", False)]
    assert kinds(4, Role.ADMIN) == [("control_exception", True)]
    assert kinds(1, Role.ADMIN) == [("control_exception", False)]


def test_development_mode_lets_the_proposer_decide(
    migrated_engine: Engine, instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    del instance_id
    propose_baseline(migrated_engine, 1)
    monkeypatch.setenv("BALUARTE_DEVELOPMENT_MODE", "true")

    [item] = client(migrated_engine, 1, Role.ANALYST).get("/api/v1/reviews").json()["items"]

    assert item["can_decide"] is True
