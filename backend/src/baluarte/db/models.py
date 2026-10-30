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
ANCESTRIES = ("complete", "incomplete")
CAPABILITY_STATUSES = ("not_required", "supported", "unsupported", "unknown")
FINDING_RESOLUTIONS = ("passed", "no_longer_applies")
EXCEPTION_STATES = ("requested", "approved", "rejected", "revoked", "expired")
# Longest an exception may last, from its request.
MAX_EXCEPTION_DAYS = 365
PLAN_STATES = ("draft", "submitted", "approved", "rejected", "expired")
# Longest a remediation plan stays valid, from its creation, and the most steps it may hold.
MAX_PLAN_HOURS = 72
MAX_PLAN_STEPS = 100


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


class EvaluationRun(Base):
    """One attempt at evaluating every project's effective policies (BAL-021)."""

    __tablename__ = "evaluation_run"
    __table_args__ = (
        Index("ix_evaluation_run_started", "instance_id", "started_at"),
        one_of("status", RUN_STATUSES),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="finished_unless_running"
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    collection_job_id: Mapped[int | None] = mapped_column(ForeignKey("collection_job.id"))
    attempt: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default="running")
    started_at: Mapped[datetime] = utc_now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    projects: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Results by outcome and finding transitions, e.g. {"pass": 3, "opened": 1}.
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)


class Evaluation(Base):
    """Outcome of one rule for one project, linked to the observation it judged.

    Rows cannot be updated. ``pass`` and ``fail`` always reference an observation; ``unknown``
    and ``unsupported`` reference the project's latest observation when there is one.
    """

    __tablename__ = "evaluation"
    __table_args__ = (
        Index("ix_evaluation_policy_rule", "policy_id", "rule_id"),
        Index("ix_evaluation_project_rule", "project_id", "policy_name", "rule_id", "evaluated_at"),
        Index("ix_evaluation_run", "evaluation_run_id"),
        one_of("outcome", OUTCOMES),
        one_of("severity", SEVERITIES),
        one_of("ancestry", ANCESTRIES),
        one_of("capability_status", CAPABILITY_STATUSES),
        CheckConstraint(
            "(outcome = 'unknown') = (cardinality(unknown_reasons) > 0)",
            name="unknown_has_reasons",
        ),
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'excepted') OR observed IS NULL",
            name="observed_only_when_decided",
        ),
        CheckConstraint(
            "outcome NOT IN ('pass', 'fail', 'excepted') OR observation_id IS NOT NULL",
            name="decided_has_evidence",
        ),
        CheckConstraint(
            "(outcome = 'excepted') = (exception_id IS NOT NULL)", name="excepted_has_exception"
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    evaluation_run_id: Mapped[int | None] = mapped_column(ForeignKey("evaluation_run.id"))
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("observation.id"), index=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    policy_name: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    rule_type: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    # Every reason an unknown result could not be decided, most fundamental first.
    unknown_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    expected: Mapped[Any] = mapped_column(JSONB)
    observed: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    source: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capability_name: Mapped[str | None] = mapped_column(Text)
    capability_status: Mapped[str] = mapped_column(Text)
    collection_kind: Mapped[str] = mapped_column(Text)
    collection_status: Mapped[str] = mapped_column(Text)
    collection_last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ancestry: Mapped[str] = mapped_column(Text)
    # The approved exception that waived a failure; set exactly when ``outcome = excepted``.
    exception_id: Mapped[int | None] = mapped_column(ForeignKey("policy_exception.id"))
    evaluated_at: Mapped[datetime] = utc_now()


class Finding(Base):
    """A rule a project failed, followed across policy versions until it passes again.

    Keyed by policy name, not version, so a new version of the same baseline continues the
    finding. ``latest_outcome`` is the latest evaluation's outcome: an ``unknown`` result
    never resolves or opens a finding, it only shows that the last check could not decide.
    """

    __tablename__ = "finding"
    __table_args__ = (
        UniqueConstraint("project_id", "policy_name", "rule_id"),
        Index("ix_finding_state", "state", "id"),
        one_of("severity", SEVERITIES),
        one_of("state", FINDING_STATES),
        one_of("latest_outcome", OUTCOMES),
        one_of("resolution", FINDING_RESOLUTIONS),
        CheckConstraint(
            "(state = 'resolved') = (resolved_at IS NOT NULL)", name="resolved_has_time"
        ),
        CheckConstraint(
            "(state = 'resolved') = (resolution IS NOT NULL)", name="resolved_has_resolution"
        ),
        CheckConstraint(
            "(state = 'excepted') = (exception_id IS NOT NULL)", name="excepted_has_exception"
        ),
    )

    id: Mapped[int] = pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    # The policy version of the latest evaluation.
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"))
    policy_name: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    latest_evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluation.id"))
    latest_outcome: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="open")
    first_seen_at: Mapped[datetime] = utc_now()
    # When an evaluation last found the rule failing.
    last_seen_at: Mapped[datetime] = utc_now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    # The exception waiving the failure while ``state = excepted``.
    exception_id: Mapped[int | None] = mapped_column(ForeignKey("policy_exception.id"))


class PolicyException(Base):
    """A time-limited waiver of one rule for one project, approved by someone else.

    ``requested`` becomes ``approved`` or ``rejected``; ``requested`` or ``approved`` becomes
    ``revoked``, or ``expired`` once ``expires_at`` has passed. A trigger enforces those
    transitions, keeps what was requested unchanged and rejects ``DELETE``; the audit trail
    records who did each. An approved exception whose ``expires_at`` has passed is in force no
    longer, whether or not its row has been marked ``expired`` yet.
    """

    __tablename__ = "policy_exception"
    __table_args__ = (
        Index(
            "uq_policy_exception_active",
            "project_id",
            "policy_name",
            "rule_id",
            unique=True,
            postgresql_where=text("state IN ('requested', 'approved')"),
        ),
        one_of("state", EXCEPTION_STATES),
        CheckConstraint("length(justification) > 0", name="justified"),
        CheckConstraint(
            f"expires_at > requested_at AND expires_at <= requested_at + "
            f"interval '{MAX_EXCEPTION_DAYS * 24} hours'",
            name="expiry_bounded",
        ),
        CheckConstraint(
            "decided_by_user_id IS NULL OR decided_by_user_id <> requested_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)", name="decision_complete"
        ),
        CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name="decided_has_decision",
        ),
        CheckConstraint(
            "(state IN ('revoked', 'expired')) = (ended_at IS NOT NULL)", name="ended_has_time"
        ),
        CheckConstraint(
            "(state = 'revoked') = (ended_by_user_id IS NOT NULL)", name="revoked_has_user"
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), index=True)
    policy_name: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    justification: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="requested")
    requested_by_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    requested_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # Set when revoked (with the user) or expired (by the system).
    ended_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_note: Mapped[str | None] = mapped_column(Text)


class RemediationPlan(Base):
    """A dry-run remediation plan (BAL-031): the GitLab changes that would fix open findings.

    The plan document and its digest never change. ``draft`` becomes ``submitted`` (by its
    creator), then ``approved`` or ``rejected`` by someone else; ``draft``, ``submitted`` or
    ``approved`` becomes ``expired`` once ``expires_at`` has passed. Approval records the digest
    the approver reviewed, which must equal the plan's. A trigger enforces the transitions,
    keeps the plan unchanged and rejects ``DELETE``. Nothing here changes GitLab: execution is
    BAL-032.
    """

    __tablename__ = "remediation_plan"
    __table_args__ = (
        Index("ix_remediation_plan_state", "instance_id", "state", "id"),
        one_of("state", PLAN_STATES),
        sha256_hex("plan_digest"),
        CheckConstraint(
            f"expires_at > created_at AND expires_at <= created_at + "
            f"interval '{MAX_PLAN_HOURS} hours'",
            name="expiry_bounded",
        ),
        CheckConstraint(f"steps BETWEEN 1 AND {MAX_PLAN_STEPS}", name="steps_bounded"),
        CheckConstraint("skipped >= 0", name="skipped_non_negative"),
        CheckConstraint(
            "decided_by_user_id IS NULL OR decided_by_user_id <> created_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)", name="decision_complete"
        ),
        CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name="decided_has_decision",
        ),
        CheckConstraint(
            "state NOT IN ('submitted', 'approved', 'rejected') OR submitted_at IS NOT NULL",
            name="submitted_has_time",
        ),
        CheckConstraint(
            "state <> 'approved' OR approved_digest IS NOT NULL", name="approved_has_digest"
        ),
        CheckConstraint(
            "state NOT IN ('draft', 'submitted', 'rejected') OR approved_digest IS NULL",
            name="unapproved_has_no_digest",
        ),
        CheckConstraint(
            "approved_digest IS NULL OR approved_digest = plan_digest",
            name="approval_binds_digest",
        ),
        CheckConstraint("(state = 'expired') = (ended_at IS NOT NULL)", name="expired_has_time"),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    state: Mapped[str] = mapped_column(Text, server_default="draft")
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The canonical plan document; ``plan_digest`` is the SHA-256 of its canonical JSON.
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB)
    plan_digest: Mapped[str] = mapped_column(Text)
    steps: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # The digest the approver reviewed; equal to ``plan_digest`` when approved.
    approved_digest: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
