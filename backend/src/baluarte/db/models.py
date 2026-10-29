"""Relational schema. See docs/DATA_MODEL.md.

Status-like columns are text with CHECK constraints rather than PostgreSQL enum types, so adding
a value is a constraint change rather than an enum migration. All timestamps are timezone-aware
and stored in UTC.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baluarte.db.base import Base

VISIBILITIES = ("private", "internal", "public")
NAMESPACE_KINDS = ("group", "user")
INVENTORY_STATES = ("active", "missing", "inaccessible")
JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled")
COLLECTION_STATUSES = ("ok", "partial", "forbidden", "not_found", "unsupported", "failed")
OUTCOMES = ("pass", "fail", "unknown", "unsupported", "excepted")
TARGET_TYPES = ("instance", "group", "project")
SEVERITIES = ("low", "medium", "high", "critical")
FINDING_STATES = ("open", "resolved", "excepted")
EXPORT_STATES = ("queued", "running", "succeeded", "failed")
ROLES = ("admin", "analyst", "auditor")
RUN_KINDS = ("capabilities", "groups", "projects")
RUN_STATUSES = ("running", "succeeded", "partial", "failed")


def one_of(column: str, values: Iterable[str]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"{column}_allowed")


def sha256_hex(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[0-9a-f]{{64}}$'", name=f"{column}_sha256")


def pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def utc_now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class GitLabInstance(Base):
    __tablename__ = "gitlab_instance"

    id: Mapped[int] = pk()
    base_url: Mapped[str] = mapped_column(Text, unique=True)
    version: Mapped[str | None] = mapped_column(Text)
    edition: Mapped[str | None] = mapped_column(Text)
    capability_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_now()


class GitLabGroup(Base):
    __tablename__ = "gitlab_group"
    __table_args__ = (
        UniqueConstraint("instance_id", "gitlab_id"),
        Index("ix_gitlab_group_parent", "instance_id", "parent_gitlab_id"),
        one_of("visibility", VISIBILITIES),
        one_of("state", INVENTORY_STATES),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    gitlab_id: Mapped[int] = mapped_column(BigInteger)
    parent_gitlab_id: Mapped[int | None] = mapped_column(BigInteger)
    full_path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="active")
    first_seen_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()
    # Set when the last attempt to confirm this resource failed; cleared when it is seen again.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitLabProject(Base):
    __tablename__ = "gitlab_project"
    __table_args__ = (
        UniqueConstraint("instance_id", "gitlab_id"),
        Index("ix_gitlab_project_namespace", "instance_id", "namespace_gitlab_id"),
        one_of("visibility", VISIBILITIES),
        one_of("state", INVENTORY_STATES),
        one_of("namespace_kind", NAMESPACE_KINDS),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    gitlab_id: Mapped[int] = mapped_column(BigInteger)
    namespace_gitlab_id: Mapped[int] = mapped_column(BigInteger)
    # A group or a user (personal project); null when unknown, e.g. not yet rediscovered.
    namespace_kind: Mapped[str | None] = mapped_column(Text)
    path_with_namespace: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    default_branch: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="active")
    first_seen_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()
    # Set when the last attempt to confirm this resource failed; cleared when it is seen again.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectionJob(Base):
    """Background job, leased by workers. See baluarte.jobs and ADR-0002."""

    __tablename__ = "collection_job"
    __table_args__ = (
        Index("ix_collection_job_claimable", "state", "run_after"),
        Index(
            "ix_collection_job_lease",
            "lease_until",
            postgresql_where=text("state = 'running'"),
        ),
        one_of("state", JOB_STATES),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("attempt <= max_attempts", name="attempt_within_max"),
        CheckConstraint(
            "(state = 'running') = (lease_owner IS NOT NULL)"
            " AND (state = 'running') = (lease_until IS NOT NULL)",
            name="lease_iff_running",
        ),
    )

    id: Mapped[int] = pk()
    kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="queued")
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    run_after: Mapped[datetime] = utc_now()
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectionRun(Base):
    """One attempt at a collection job, for freshness and error reporting (BAL-015)."""

    __tablename__ = "collection_run"
    __table_args__ = (
        Index("ix_collection_run_kind_started", "instance_id", "kind", "started_at"),
        one_of("kind", RUN_KINDS),
        one_of("status", RUN_STATUSES),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="finished_unless_running"
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    kind: Mapped[str] = mapped_column(Text)
    collection_job_id: Mapped[int | None] = mapped_column(ForeignKey("collection_job.id"))
    # The job attempt this run belongs to; a later attempt closes this run if it was cut short.
    attempt: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default="running")
    started_at: Mapped[datetime] = utc_now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Reconciliation outcomes, e.g. {"missing": 1, "unchanged": 2}.
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)


class Policy(Base):
    """An immutable policy version. A changed baseline is a new row with a higher version."""

    __tablename__ = "policy"
    __table_args__ = (
        UniqueConstraint("name", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        sha256_hex("yaml_digest"),
    )

    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    source_yaml: Mapped[str] = mapped_column(Text)
    yaml_digest: Mapped[str] = mapped_column(Text)
    effective_at: Mapped[datetime] = utc_now()
    created_at: Mapped[datetime] = utc_now()


class PolicyAssignment(Base):
    """Pins one immutable policy version to the instance, a group or a project (BAL-018).

    ``target_id`` is the GitLab ID of a group or project, or the ``gitlab_instance`` row ID for
    the instance. A target holds at most one version of each policy name; how assignments are
    inherited is in ``baluarte.policy.assignments``. A trigger (migration 0006) rejects a
    ``policy_name`` that differs from the name of the version ``policy_id`` refers to.
    """

    __tablename__ = "policy_assignment"
    __table_args__ = (
        UniqueConstraint(
            "instance_id",
            "target_type",
            "target_id",
            "policy_name",
            name="uq_policy_assignment_target_policy_name",
        ),
        Index("ix_policy_assignment_policy_id", "policy_id"),
        one_of("target_type", TARGET_TYPES),
        CheckConstraint(
            "target_type <> 'instance' OR target_id = instance_id", name="instance_target"
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    policy_name: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = utc_now()


class Observation(Base):
    """Evidence collected from GitLab at a point in time. Rows cannot be updated.

    A row with neither project nor group is an instance-level observation (e.g. capabilities).
    """

    __tablename__ = "observation"
    __table_args__ = (
        Index("ix_observation_project_collected", "project_id", "collected_at"),
        Index("ix_observation_group_collected", "group_id", "collected_at"),
        Index(
            "ix_observation_instance_collected",
            "instance_id",
            "source",
            "collected_at",
            postgresql_where=text("project_id IS NULL AND group_id IS NULL"),
        ),
        one_of("collection_status", COLLECTION_STATUSES),
        sha256_hex("payload_digest"),
        CheckConstraint("project_id IS NULL OR group_id IS NULL", name="single_target"),
        CheckConstraint("(payload IS NULL) = (payload_digest IS NULL)", name="payload_has_digest"),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("gitlab_project.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("gitlab_group.id"))
    collection_job_id: Mapped[int | None] = mapped_column(ForeignKey("collection_job.id"))
    collection_run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_run.id"))
    source: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collection_status: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    payload_digest: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evaluation(Base):
    """Outcome of one rule against one observation. Rows cannot be updated."""

    __tablename__ = "evaluation"
    __table_args__ = (
        Index("ix_evaluation_policy_rule", "policy_id", "rule_id"),
        one_of("outcome", OUTCOMES),
    )

    id: Mapped[int] = pk()
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"), index=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    rule_id: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    expected: Mapped[Any] = mapped_column(JSONB)
    observed: Mapped[Any | None] = mapped_column(JSONB)
    capability_status: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime] = utc_now()


class Finding(Base):
    """Current operational state of a rule for a project, pointing at its latest evaluation."""

    __tablename__ = "finding"
    __table_args__ = (
        UniqueConstraint("project_id", "policy_id", "rule_id"),
        one_of("severity", SEVERITIES),
        one_of("state", FINDING_STATES),
    )

    id: Mapped[int] = pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    rule_id: Mapped[str] = mapped_column(Text)
    latest_evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluation.id"))
    severity: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="open")
    first_seen_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Administrative audit trail. Rows cannot be updated or deleted."""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_occurred", "occurred_at"),
        sha256_hex("details_digest"),
    )

    id: Mapped[int] = pk()
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    details_digest: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = utc_now()


class ExportJob(Base):
    __tablename__ = "export_job"
    __table_args__ = (
        one_of("status", EXPORT_STATES),
        sha256_hex("manifest_digest"),
        CheckConstraint("period_end > period_start", name="period_ordered"),
    )

    id: Mapped[int] = pk()
    requested_by: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    manifest_digest: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppUser(Base):
    """A person who has signed in through OIDC, identified by issuer and subject."""

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: Mapped[int] = pk()
    issuer: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    """Server-side browser session. Only the SHA-256 digest of the cookie value is stored.

    Roles are resolved from identity provider claims at sign-in and fixed for the session.
    """

    __tablename__ = "user_session"
    __table_args__ = (
        sha256_hex("token_digest"),
        CheckConstraint(
            f"cardinality(roles) > 0 AND roles <@ ARRAY[{', '.join(map(repr, ROLES))}]::text[]",
            name="roles_allowed",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )

    id: Mapped[int] = pk()
    token_digest: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OidcLogin(Base):
    """A sign-in that has been sent to the identity provider and not yet completed.

    Keyed by the SHA-256 digest of the ``state`` parameter; each row is used at most once.
    """

    __tablename__ = "oidc_login"
    __table_args__ = (sha256_hex("state_digest"),)

    state_digest: Mapped[str] = mapped_column(Text, primary_key=True)
    nonce: Mapped[str] = mapped_column(Text)
    code_verifier: Mapped[str] = mapped_column(Text)
    return_to: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
