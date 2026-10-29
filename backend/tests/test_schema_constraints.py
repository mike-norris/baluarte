import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Engine, delete, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from baluarte.db.models import (
    AppUser,
    AuditEvent,
    Evaluation,
    ExportJob,
    GitLabInstance,
    GitLabProject,
    Observation,
    Policy,
    UserSession,
)

pytestmark = pytest.mark.db


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine) as session:
        yield session


@pytest.fixture
def instance(session: Session) -> GitLabInstance:
    instance = GitLabInstance(base_url="https://gitlab.example.internal")
    session.add(instance)
    session.commit()
    return instance


def add_project(session: Session, instance: GitLabInstance, gitlab_id: int) -> GitLabProject:
    project = GitLabProject(
        instance_id=instance.id,
        gitlab_id=gitlab_id,
        namespace_gitlab_id=10,
        path_with_namespace=f"group/project-{gitlab_id}",
        name=f"project-{gitlab_id}",
        visibility="private",
    )
    session.add(project)
    session.commit()
    return project


def add_observation(session: Session, instance: GitLabInstance) -> Observation:
    payload = {"visibility": "private"}
    observation = Observation(
        instance_id=instance.id,
        source="GET /api/v4/projects/1",
        collected_at=datetime.now(UTC),
        collection_status="ok",
        payload=payload,
        payload_digest=digest(payload),
    )
    session.add(observation)
    session.commit()
    return observation


def test_project_identity_is_unique_per_instance(
    session: Session, instance: GitLabInstance
) -> None:
    add_project(session, instance, gitlab_id=1)

    with pytest.raises(IntegrityError, match="uq_gitlab_project_instance_id_gitlab_id"):
        add_project(session, instance, gitlab_id=1)


def test_same_gitlab_id_on_another_instance_is_allowed(
    session: Session, instance: GitLabInstance
) -> None:
    other = GitLabInstance(base_url="https://gitlab-2.example.internal")
    session.add(other)
    session.commit()

    add_project(session, instance, gitlab_id=1)
    add_project(session, other, gitlab_id=1)


def test_rejects_unknown_visibility(session: Session, instance: GitLabInstance) -> None:
    session.add(
        GitLabProject(
            instance_id=instance.id,
            gitlab_id=2,
            namespace_gitlab_id=10,
            path_with_namespace="group/p",
            name="p",
            visibility="secret",
        )
    )
    with pytest.raises(IntegrityError, match="ck_gitlab_project_visibility_allowed"):
        session.commit()


def evaluation(
    session: Session, instance: GitLabInstance, outcome: str, **overrides: Any
) -> Evaluation:
    project = add_project(session, instance, 1)
    observation = add_observation(session, instance)
    policy = Policy(name="baseline", version=1, source_yaml="x", yaml_digest=digest("x"))
    session.add(policy)
    session.commit()
    values: dict[str, Any] = {
        "instance_id": instance.id,
        "project_id": project.id,
        "observation_id": observation.id,
        "policy_id": policy.id,
        "policy_name": "baseline",
        "rule_id": "project-visibility",
        "rule_type": "gitlab.project.visibility",
        "severity": "medium",
        "outcome": outcome,
        "reason": "test",
        "unknown_reasons": ["value_unknown"] if outcome == "unknown" else [],
        "expected": {"allowed": ["private"]},
        "observed": "private" if outcome in ("pass", "fail") else None,
        "source": "gitlab_project.visibility",
        "capability_status": "not_required",
        "collection_kind": "projects",
        "collection_status": "fresh",
        "ancestry": "complete",
    }
    values.update(overrides)
    return Evaluation(**values)


@pytest.mark.parametrize("outcome", ["pass", "fail", "unknown", "unsupported", "excepted"])
def test_accepts_every_documented_outcome(
    session: Session, instance: GitLabInstance, outcome: str
) -> None:
    session.add(evaluation(session, instance, outcome))
    session.commit()


def test_rejects_undocumented_outcome(session: Session, instance: GitLabInstance) -> None:
    session.add(evaluation(session, instance, "compliant"))
    with pytest.raises(IntegrityError, match="ck_evaluation_outcome_allowed"):
        session.commit()


@pytest.mark.parametrize(
    ("outcome", "overrides", "constraint"),
    [
        ("pass", {"observation_id": None}, "ck_evaluation_decided_has_evidence"),
        ("fail", {"observation_id": None}, "ck_evaluation_decided_has_evidence"),
        ("unknown", {"unknown_reasons": []}, "ck_evaluation_unknown_has_reasons"),
        ("pass", {"unknown_reasons": ["value_unknown"]}, "ck_evaluation_unknown_has_reasons"),
        ("unknown", {"observed": "private"}, "ck_evaluation_observed_only_when_decided"),
        ("pass", {"severity": "urgent"}, "ck_evaluation_severity_allowed"),
        ("pass", {"ancestry": "partial"}, "ck_evaluation_ancestry_allowed"),
        ("pass", {"capability_status": "maybe"}, "ck_evaluation_capability_status_allowed"),
    ],
)
def test_evaluation_invariants(
    session: Session,
    instance: GitLabInstance,
    outcome: str,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    session.add(evaluation(session, instance, outcome, **overrides))
    with pytest.raises(IntegrityError, match=constraint):
        session.commit()


def test_unknown_needs_no_observation(session: Session, instance: GitLabInstance) -> None:
    session.add(evaluation(session, instance, "unknown", observation_id=None))
    session.commit()


def test_evaluation_policy_name_must_match_the_version(
    session: Session, instance: GitLabInstance
) -> None:
    session.add(evaluation(session, instance, "pass", policy_name="another"))
    with pytest.raises(DBAPIError, match="policy_name does not match"):
        session.commit()


def test_rejects_malformed_digest(session: Session, instance: GitLabInstance) -> None:
    session.add(
        Observation(
            instance_id=instance.id,
            source="GET /api/v4/projects/1",
            collected_at=datetime.now(UTC),
            collection_status="ok",
            payload={"a": 1},
            payload_digest="not-a-sha256",
        )
    )
    with pytest.raises(IntegrityError, match="ck_observation_payload_digest_sha256"):
        session.commit()


def test_payload_requires_digest(session: Session, instance: GitLabInstance) -> None:
    session.add(
        Observation(
            instance_id=instance.id,
            source="GET /api/v4/projects/1",
            collected_at=datetime.now(UTC),
            collection_status="ok",
            payload={"a": 1},
        )
    )
    with pytest.raises(IntegrityError, match="ck_observation_payload_has_digest"):
        session.commit()


def test_failed_collection_without_payload_is_recorded(
    session: Session, instance: GitLabInstance
) -> None:
    session.add(
        Observation(
            instance_id=instance.id,
            source="GET /api/v4/projects/1/protected_branches",
            collected_at=datetime.now(UTC),
            collection_status="forbidden",
            error="403 Forbidden",
        )
    )
    session.commit()


def test_observation_cannot_be_updated(session: Session, instance: GitLabInstance) -> None:
    observation = add_observation(session, instance)

    with pytest.raises(DBAPIError, match="immutable evidence"):
        session.execute(
            update(Observation)
            .where(Observation.id == observation.id)
            .values(collection_status="failed")
        )


def test_observation_can_be_deleted_for_retention(
    session: Session, instance: GitLabInstance
) -> None:
    observation = add_observation(session, instance)

    session.execute(delete(Observation).where(Observation.id == observation.id))
    session.commit()

    assert session.get(Observation, observation.id) is None


def test_audit_events_are_append_only(session: Session) -> None:
    details = {"field": "baseline"}
    event_id = session.execute(
        insert(AuditEvent)
        .values(
            actor="admin@example.internal",
            action="policy.update",
            resource_type="policy",
            details=details,
            details_digest=digest(details),
        )
        .returning(AuditEvent.id)
    ).scalar_one()
    session.commit()

    with pytest.raises(DBAPIError, match="immutable evidence"):
        session.execute(update(AuditEvent).where(AuditEvent.id == event_id).values(actor="x"))
    session.rollback()
    with pytest.raises(DBAPIError, match="immutable evidence"):
        session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))


def test_timestamps_round_trip_as_utc_instants(session: Session, instance: GitLabInstance) -> None:
    collected = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    session.add(
        Observation(
            instance_id=instance.id,
            source="GET /api/v4/version",
            collected_at=collected,
            collection_status="ok",
            payload={},
            payload_digest=digest({}),
        )
    )
    session.commit()

    stored = session.scalars(select(Observation.collected_at)).one()

    assert stored == collected
    assert stored.utcoffset() is not None


def test_export_period_must_be_ordered(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(ExportJob(requested_by="auditor", scope={}, period_start=now, period_end=now))
    with pytest.raises(IntegrityError, match="ck_export_job_period_ordered"):
        session.commit()


def _user(db: Session) -> AppUser:
    user = AppUser(issuer="https://idp.example.test", subject="user-1")
    db.add(user)
    db.flush()
    return user


@pytest.mark.parametrize("roles", [[], ["root"], ["admin", "superuser"]])
def test_session_roles_are_constrained(migrated_engine: Engine, roles: list[str]) -> None:
    with Session(migrated_engine) as db:
        user = _user(db)
        db.add(
            UserSession(
                token_digest="a" * 64,
                user_id=user.id,
                roles=roles,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()


def test_session_token_must_be_a_digest(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        user = _user(db)
        db.add(
            UserSession(
                token_digest="raw-session-token",
                user_id=user.id,
                roles=["admin"],
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()


def test_user_identity_is_unique_per_issuer(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        _user(db)
        db.add(AppUser(issuer="https://idp.example.test", subject="user-1"))
        with pytest.raises(IntegrityError):
            db.flush()
