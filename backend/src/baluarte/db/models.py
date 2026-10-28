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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baluarte.db.base import Base

VISIBILITIES = ("private", "internal", "public")
INVENTORY_STATES = ("active", "missing", "inaccessible")
JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled")
COLLECTION_STATUSES = ("ok", "partial", "forbidden", "not_found", "unsupported", "failed")
OUTCOMES = ("pass", "fail", "unknown", "unsupported", "excepted")
TARGET_TYPES = ("instance", "group", "project")
SEVERITIES = ("low", "medium", "high", "critical")
FINDING_STATES = ("open", "resolved", "excepted")
EXPORT_STATES = ("queued", "running", "succeeded", "failed")


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


class GitLabProject(Base):
    __tablename__ = "gitlab_project"
    __table_args__ = (
        UniqueConstraint("instance_id", "gitlab_id"),
        Index("ix_gitlab_project_namespace", "instance_id", "namespace_gitlab_id"),
        one_of("visibility", VISIBILITIES),
        one_of("state", INVENTORY_STATES),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    gitlab_id: Mapped[int] = mapped_column(BigInteger)
    namespace_gitlab_id: Mapped[int] = mapped_column(BigInteger)
    path_with_namespace: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    default_branch: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="active")
    first_seen_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()


class CollectionJob(Base):
    __tablename__ = "collection_job"
    __table_args__ = (
        Index("ix_collection_job_claimable", "state", "run_after"),
        one_of("state", JOB_STATES),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
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
    """Scopes a policy to a target. Inheritance and precedence rules are defined in BAL-018."""

    __tablename__ = "policy_assignment"
    __table_args__ = (
        UniqueConstraint("policy_id", "target_type", "target_id"),
        one_of("target_type", TARGET_TYPES),
    )

    id: Mapped[int] = pk()
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[int] = mapped_column(BigInteger)
    precedence: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = utc_now()


class Observation(Base):
    """Evidence collected from GitLab at a point in time. Rows cannot be updated.

    A row with neither project nor group is an instance-level observation (e.g. capabilities).
    """

    __tablename__ = "observation"
    __table_args__ = (
        Index("ix_observation_project_collected", "project_id", "collected_at"),
        Index("ix_observation_group_collected", "group_id", "collected_at"),
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
    source: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collection_status: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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
