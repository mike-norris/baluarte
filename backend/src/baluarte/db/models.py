"""Relational schema. See docs/DATA_MODEL.md.

Status-like columns are text with CHECK constraints rather than PostgreSQL enum types, so adding
a value is a constraint change rather than an enum migration. All timestamps are timezone-aware
and stored in UTC.
"""

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
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
RUN_KINDS = ("capabilities", "groups", "projects", "security_policies", "changes", "labels")
SECURITY_POLICY_TYPES = (
    "scan_execution",
    "approval",
    "pipeline_execution",
    "vulnerability_management",
)
# Where a discovered security policy lives: its security policy project, or only the group or
# project it is linked to when Baluarte cannot see which policy project that is.
POLICY_ORIGINS = ("policy_project", "linked_source")
RUN_STATUSES = ("running", "succeeded", "partial", "failed")
ANCESTRIES = ("complete", "incomplete")
CAPABILITY_STATUSES = ("not_required", "supported", "unsupported", "unknown")
FINDING_RESOLUTIONS = ("passed", "no_longer_applies", "project_archived", "project_excluded")
EXCEPTION_STATES = ("requested", "approved", "rejected", "revoked", "expired")
# Longest an exception may last, from its request.
MAX_EXCEPTION_DAYS = 365
POLICY_CHANGE_KINDS = ("version", "assign", "unassign")
POLICY_CHANGE_STATES = ("proposed", "approved", "rejected")
COMPLIANCE_EXCEPTION_STATES = ("proposed", "approved", "rejected", "revoked")
MAPPING_CHANGE_KINDS = ("map", "unmap", "not_applicable", "manual", "withdraw")
MAPPING_CHANGE_STATES = ("proposed", "active", "rejected", "withdrawn")
MAPPING_FRAMEWORKS = ("soc2", "iso27001", "nist_800_53_moderate")
# Length of a control mapping change's justification, and of its evidence reference.
MIN_JUSTIFICATION = 20
MAX_JUSTIFICATION = 2000
SCOPE_TARGET_TYPES = ("project", "group", "personal_namespaces")
SCOPE_EXCLUSION_STATES = ("proposed", "active", "rejected", "ended")
MAX_EVIDENCE_REF = 500
# Longest a compliance exception may last, from its request, and the length of its texts.
MAX_COMPLIANCE_EXCEPTION_DAYS = 365
MIN_STATEMENT = 10
MAX_STATEMENT = 2000
PLAN_STATES = ("draft", "submitted", "approved", "rejected", "expired")
# Longest a remediation plan stays valid, from its creation, and the most steps it may hold.
MAX_PLAN_HOURS = 72
MAX_PLAN_STEPS = 100
# Sections and secrets that can be stored through the web UI (ADR-0008).
SETTING_KEYS = (
    "oidc",
    "gitlab",
    "governance",
    "operations",
    "audit_shipping",
    "change_evidence",
    "label_write_token",
)
SECRET_NAMES = (
    "oidc_client_secret",
    "gitlab_token",
    "audit_shipping_es_api_key",
    "audit_shipping_es_password",
    "label_write_token",
)
BACKUP_STATUSES = ("succeeded", "failed")
CHANGE_KINDS = ("merge_request", "direct_push", "deployment")
CHANGE_OUTCOMES = ("pass", "fail", "unknown")
LABEL_TAXONOMY_STATES = ("proposed", "approved", "rejected")
LABEL_PLAN_STATES = (
    "draft",
    "submitted",
    "approved",
    "rejected",
    "applying",
    "applied",
    "failed",
    "expired",
)
# A label plan is valid this long from its creation, and holds at most this many steps.
LABEL_PLAN_HOURS = 24
MAX_LABEL_PLAN_STEPS = 1000
MAX_TAXONOMY_JUSTIFICATION = 2000


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
    # Two-factor enforcement for the group's members; null when GitLab did not report it.
    require_two_factor_authentication: Mapped[bool | None] = mapped_column(Boolean)
    # Hours members have to set up two-factor authentication once it is required.
    two_factor_grace_period: Mapped[int | None] = mapped_column(Integer)


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


class GitLabSecurityPolicy(Base):
    """A GitLab security policy seen by discovery: current state, rebuilt each complete run.

    One row per policy, keyed by where it lives (``origin_path``) rather than per group it is
    linked to, so a policy project linked to three groups gives one row per policy. The evidence
    is the per-group and per-project observations; this table is the catalogue built from them.
    """

    __tablename__ = "gitlab_security_policy"
    __table_args__ = (
        UniqueConstraint(
            "instance_id",
            "policy_type",
            "origin_path",
            "name",
            name="uq_gitlab_security_policy_key",
        ),
        one_of("policy_type", SECURITY_POLICY_TYPES),
        one_of("origin_kind", POLICY_ORIGINS),
        one_of("state", ("active", "missing")),
        sha256_hex("yaml_digest"),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    policy_type: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    origin_kind: Mapped[str] = mapped_column(Text)
    # The security policy project's path, or for ``linked_source`` the group or project path.
    origin_path: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool | None] = mapped_column(Boolean)
    yaml: Mapped[str | None] = mapped_column(Text)
    yaml_digest: Mapped[str | None] = mapped_column(Text)
    # When GitLab says the policy YAML last changed.
    gitlab_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Normalized policy_scope, and the groups and projects the policy is linked through.
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    linked_from: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    state: Mapped[str] = mapped_column(Text, server_default="active")
    first_seen_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()
    last_collection_run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_run.id"))


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
            "ix_observation_project_source_ref",
            "project_id",
            "source",
            "source_ref",
            "collected_at",
        ),
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
        Index("ix_evaluation_observation_ids", "observation_ids", postgresql_using="gin"),
        sha256_hex("evidence_digest"),
        CheckConstraint(
            "(observation_ids IS NULL) = (evidence_digest IS NULL)",
            name="observation_ids_have_digest",
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
    # A result judged from many observations (the change rules): every observation it judged,
    # and the SHA-256 of their IDs and payload digests (``baluarte.policy.evaluation.store``).
    # ``observation_id`` then names the project's change-read observation.
    observation_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    evidence_digest: Mapped[str | None] = mapped_column(Text)


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


class PolicyChange(Base):
    """A change to the baselines in force: a new version, an assignment or an unassignment.

    ``policy`` and ``policy_assignment`` hold only what is in force. A change made while review
    is required (the ``governance`` setting) is ``proposed`` and changes nothing until someone
    other than the proposer approves it; then it is applied and records its result
    (``policy_id`` of the new version, ``assignment_id`` of the new assignment). While review is
    not required a change is applied at once and recorded ``approved`` with the proposer as
    approver and ``self_approved`` set. Rows from before migration 0015 are ``migrated``.

    A trigger allows only ``proposed`` -> ``approved`` | ``rejected``, keeps what was proposed
    unchanged and rejects ``DELETE``. User IDs are recorded values, like ``audit_event.actor``,
    and are not foreign keys.
    """

    __tablename__ = "policy_change"
    __table_args__ = (
        Index(
            "uq_policy_change_proposed_version",
            "policy_name",
            unique=True,
            postgresql_where=text("state = 'proposed' AND kind = 'version'"),
        ),
        Index(
            "uq_policy_change_proposed_target",
            "instance_id",
            "target_type",
            "target_id",
            "policy_name",
            unique=True,
            postgresql_where=text("state = 'proposed' AND kind <> 'version'"),
        ),
        Index("ix_policy_change_policy_id", "policy_id"),
        Index("ix_policy_change_assignment_id", "assignment_id"),
        one_of("kind", POLICY_CHANGE_KINDS),
        one_of("state", POLICY_CHANGE_STATES),
        CheckConstraint(
            "kind <> 'version' OR (source_yaml IS NOT NULL AND yaml_digest IS NOT NULL "
            "AND instance_id IS NULL AND target_type IS NULL AND target_id IS NULL "
            "AND assignment_id IS NULL)",
            name="version_shape",
        ),
        CheckConstraint(
            "kind = 'version' OR (source_yaml IS NULL AND yaml_digest IS NULL "
            "AND instance_id IS NOT NULL AND target_type IS NOT NULL AND target_id IS NOT NULL "
            "AND policy_id IS NOT NULL)",
            name="assignment_shape",
        ),
        CheckConstraint("kind <> 'unassign' OR assignment_id IS NOT NULL", name="unassign_names"),
        CheckConstraint(
            "state <> 'approved' OR kind <> 'version' OR policy_id IS NOT NULL",
            name="approved_version_has_result",
        ),
        CheckConstraint(
            "state <> 'approved' OR kind <> 'assign' OR assignment_id IS NOT NULL",
            name="approved_assignment_has_result",
        ),
        CheckConstraint(
            "target_type IS NULL OR target_type IN ('instance', 'group', 'project')",
            name="target_type_allowed",
        ),
        CheckConstraint(
            "yaml_digest IS NULL OR yaml_digest ~ '^[0-9a-f]{64}$'", name="yaml_digest_sha256"
        ),
        CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)", name="decided_unless_proposed"
        ),
        CheckConstraint("(decided_at IS NULL) = (decided_by IS NULL)", name="decision_complete"),
        CheckConstraint(
            "migrated OR (proposed_by_user_id IS NOT NULL "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL))",
            name="users_recorded",
        ),
        CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint("review_required OR state = 'approved'", name="unreviewed_is_applied"),
        CheckConstraint(
            "self_approved = (NOT review_required AND NOT migrated)",
            name="self_approved_when_unreviewed",
        ),
        CheckConstraint(
            "NOT self_approved OR decided_by_user_id = proposed_by_user_id",
            name="self_approved_by_proposer",
        ),
        CheckConstraint("NOT migrated OR NOT review_required", name="migrated_unreviewed"),
        CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0", name="rejection_explained"
        ),
    )

    id: Mapped[int] = pk()
    kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    policy_name: Mapped[str] = mapped_column(Text)
    # version: the proposed source; set on approval, policy_id is the version it became.
    source_yaml: Mapped[str | None] = mapped_column(Text)
    yaml_digest: Mapped[str | None] = mapped_column(Text)
    # The latest version in force when the change was proposed, for comparison.
    base_version: Mapped[int | None] = mapped_column(Integer)
    # assign: the version to pin; unassign: the version pinned.
    policy_id: Mapped[int | None] = mapped_column(ForeignKey("policy.id"))
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("gitlab_instance.id"))
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    # unassign: the assignment to remove; assign: set on approval to the assignment made.
    # Not a foreign key: assignments are deleted when removed or replaced.
    assignment_id: Mapped[int | None] = mapped_column(BigInteger)
    review_required: Mapped[bool] = mapped_column(Boolean)
    self_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    migrated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    proposed_by: Mapped[str] = mapped_column(Text)
    proposed_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    proposed_at: Mapped[datetime] = utc_now()
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class ControlMappingChange(Base):
    """An organization's change to the control mapping, with its justification.

    ``map`` and ``unmap`` add a rule type to a control or remove a default pair;
    ``not_applicable`` scopes a control out and ``manual`` records that it is met outside GitLab,
    both until ``review_by``; ``withdraw`` ends the active change ``supersedes_id``. A
    ``not_applicable`` or ``manual`` change may also supersede the control's current one, to
    renew it. Made while review is required (the ``governance`` setting) a change is
    ``proposed`` until someone other than the proposer approves it (``active``) or rejects it;
    otherwise it is ``active`` at once with ``self_approved`` set. The change it supersedes
    becomes ``withdrawn`` when it takes effect.

    A trigger allows only ``proposed`` -> ``active`` | ``rejected`` and ``active`` ->
    ``withdrawn``, changes nothing but the decision, and rejects ``DELETE``. User IDs are
    recorded values, like ``audit_event.actor``, and are not foreign keys.
    """

    __tablename__ = "control_mapping_change"
    __table_args__ = (
        Index(
            "uq_control_mapping_change_open",
            "framework",
            "control_id",
            "kind",
            text("coalesce(rule_type, '')"),
            unique=True,
            postgresql_where=text(
                "state IN ('proposed', 'active') AND kind <> 'withdraw' AND supersedes_id IS NULL"
            ),
        ),
        Index(
            "uq_control_mapping_change_withdraw",
            "supersedes_id",
            unique=True,
            postgresql_where=text("state IN ('proposed', 'active') AND supersedes_id IS NOT NULL"),
        ),
        Index("ix_control_mapping_change_control", "framework", "control_id", "id"),
        one_of("kind", MAPPING_CHANGE_KINDS),
        one_of("state", MAPPING_CHANGE_STATES),
        one_of("framework", MAPPING_FRAMEWORKS),
        CheckConstraint(
            f"length(justification) BETWEEN {MIN_JUSTIFICATION} AND {MAX_JUSTIFICATION}",
            name="justification_length",
        ),
        CheckConstraint(
            f"evidence_ref IS NULL OR length(evidence_ref) BETWEEN 1 AND {MAX_EVIDENCE_REF}",
            name="evidence_ref_length",
        ),
        CheckConstraint(
            "(rule_type IS NOT NULL) = (kind IN ('map', 'unmap'))", name="rule_type_for_pairs"
        ),
        CheckConstraint(
            "(review_by IS NOT NULL) = (kind IN ('not_applicable', 'manual'))",
            name="review_by_for_status",
        ),
        CheckConstraint("kind <> 'manual' OR evidence_ref IS NOT NULL", name="manual_has_evidence"),
        CheckConstraint(
            "kind <> 'withdraw' OR supersedes_id IS NOT NULL", name="withdraw_names_change"
        ),
        CheckConstraint(
            "supersedes_id IS NULL OR kind IN ('withdraw', 'not_applicable', 'manual')",
            name="supersedes_allowed",
        ),
        CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)", name="decided_unless_proposed"
        ),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name="decision_complete",
        ),
        CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "review_required OR state IN ('active', 'withdrawn')", name="unreviewed_is_applied"
        ),
        CheckConstraint(
            "self_approved = NOT review_required", name="self_approved_when_unreviewed"
        ),
        CheckConstraint(
            "NOT self_approved OR decided_by_user_id = proposed_by_user_id",
            name="self_approved_by_proposer",
        ),
        CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0", name="rejection_explained"
        ),
    )

    id: Mapped[int] = pk()
    framework: Mapped[str] = mapped_column(Text)
    control_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    # map and unmap: the rule type added to or removed from the control.
    rule_type: Mapped[str | None] = mapped_column(Text)
    justification: Mapped[str] = mapped_column(Text)
    # Where the evidence for a manual or not-applicable control is kept.
    evidence_ref: Mapped[str | None] = mapped_column(Text)
    # not_applicable and manual: the last day the change counts without being renewed.
    review_by: Mapped[date | None] = mapped_column(Date)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("control_mapping_change.id"))
    state: Mapped[str] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean)
    self_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The catalogue version (``baluarte.controls.catalogue.MAPPING_VERSION``) proposed against.
    mapping_version: Mapped[str] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(Text)
    proposed_by_user_id: Mapped[int] = mapped_column(BigInteger)
    proposed_at: Mapped[datetime] = utc_now()
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class ScopeExclusion(Base):
    """A project, a group or every personal-namespace project taken out of monitoring.

    Excluded projects stay in the inventory but are not evaluated or read in detail
    (``baluarte.scope``). Rows proposed together share ``batch_id`` and are decided together.
    Made while review is required (the ``governance`` setting) a row is ``proposed`` until
    someone other than the proposer approves (``active``) or rejects the batch; otherwise it is
    ``active`` at once with ``self_approved`` set. Ending an exclusion (``ended``) brings the
    projects back into monitoring and needs its own justification, but no second person.

    A trigger allows only ``proposed`` -> ``active`` | ``rejected`` and ``active`` -> ``ended``,
    changes nothing but the decision or the end, and rejects ``DELETE``. ``target_id`` is the
    GitLab ID (null for ``personal_namespaces``); ``target_path`` is its path when proposed.
    User IDs are recorded values, like ``audit_event.actor``, and are not foreign keys.
    """

    __tablename__ = "scope_exclusion"
    __table_args__ = (
        Index(
            "uq_scope_exclusion_open",
            "instance_id",
            "target_type",
            text("coalesce(target_id, 0)"),
            unique=True,
            postgresql_where=text("state IN ('proposed', 'active')"),
        ),
        Index("ix_scope_exclusion_batch", "batch_id"),
        Index("ix_scope_exclusion_state", "instance_id", "state"),
        one_of("target_type", SCOPE_TARGET_TYPES),
        one_of("state", SCOPE_EXCLUSION_STATES),
        CheckConstraint(
            "(target_id IS NULL) = (target_type = 'personal_namespaces')",
            name="target_id_unless_personal",
        ),
        CheckConstraint(
            f"length(justification) BETWEEN {MIN_JUSTIFICATION} AND {MAX_JUSTIFICATION}",
            name="justification_length",
        ),
        CheckConstraint(
            f"end_justification IS NULL OR length(end_justification) "
            f"BETWEEN {MIN_JUSTIFICATION} AND {MAX_JUSTIFICATION}",
            name="end_justification_length",
        ),
        CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)", name="decided_unless_proposed"
        ),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name="decision_complete",
        ),
        CheckConstraint("(state = 'ended') = (ended_at IS NOT NULL)", name="ended_when_ended"),
        CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL) "
            "AND (ended_at IS NULL) = (ended_by_user_id IS NULL) "
            "AND (ended_at IS NULL) = (end_justification IS NULL)",
            name="end_complete",
        ),
        CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "review_required OR state IN ('active', 'ended')", name="unreviewed_is_applied"
        ),
        CheckConstraint(
            "self_approved = NOT review_required", name="self_approved_when_unreviewed"
        ),
        CheckConstraint(
            "NOT self_approved OR decided_by_user_id = proposed_by_user_id",
            name="self_approved_by_proposer",
        ),
        CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0", name="rejection_explained"
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    target_path: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[str] = mapped_column(Text)
    justification: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean)
    self_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    proposed_by: Mapped[str] = mapped_column(Text)
    proposed_by_user_id: Mapped[int] = mapped_column(BigInteger)
    proposed_at: Mapped[datetime] = utc_now()
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_justification: Mapped[str | None] = mapped_column(Text)


class ChangeRecord(Base):
    """One change on a project's default branch: a merged merge request, a direct push, or a
    successful deployment to a production environment (``baluarte.changes``).

    What was read about it is an observation; ``observation_id`` names the latest one, and the
    earlier ones stay as its history (same project, source and ``source_ref``). ``outcome`` and
    ``reasons`` are derived from that observation with the default review and pipeline checks.
    A trigger lets an update change only those columns, ``complete`` and ``updated_at``.
    ``sha`` is the commit on the default branch (the merge or squash commit when there is one)
    or the deployed commit; ``shas`` every commit that identifies the change.
    """

    __tablename__ = "change_record"
    __table_args__ = (
        Index("ix_change_record_project_changed", "project_id", "changed_at"),
        Index("ix_change_record_instance_changed", "instance_id", "changed_at"),
        Index("ix_change_record_observation", "observation_id"),
        Index(
            "uq_change_record_merge_request",
            "project_id",
            "iid",
            unique=True,
            postgresql_where=text("kind = 'merge_request'"),
        ),
        Index(
            "uq_change_record_direct_push",
            "project_id",
            "sha",
            unique=True,
            postgresql_where=text("kind = 'direct_push'"),
        ),
        Index(
            "uq_change_record_deployment",
            "project_id",
            "deployment_id",
            unique=True,
            postgresql_where=text("kind = 'deployment'"),
        ),
        one_of("kind", CHANGE_KINDS),
        one_of("outcome", CHANGE_OUTCOMES),
        CheckConstraint(
            "(kind = 'merge_request') = (iid IS NOT NULL)", name="iid_for_merge_requests"
        ),
        CheckConstraint(
            "(kind = 'deployment') = (deployment_id IS NOT NULL)",
            name="deployment_id_for_deployments",
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    kind: Mapped[str] = mapped_column(Text)
    iid: Mapped[int | None] = mapped_column(BigInteger)
    deployment_id: Mapped[int | None] = mapped_column(BigInteger)
    sha: Mapped[str] = mapped_column(Text)
    shas: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    # Merged, committed or deployed.
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    outcome: Mapped[str] = mapped_column(Text)
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    # False when part of it could not be read for a passing reason; it is read again.
    complete: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = utc_now()
    updated_at: Mapped[datetime] = utc_now()


class ChangeCursor(Base):
    """How far a project's changes have been read, so each collection reads only what is new.

    ``history_since`` is where its change history starts: the first collection's look-back.
    """

    __tablename__ = "change_cursor"

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    history_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = utc_now()


class ComplianceException(Base):
    """A recorded, time-limited departure from the operating profile.

    While one is in force (``approved`` and before ``expires_on``, UTC) a control it names may be
    unmet: a setting may be saved below the profile's minimum, and the posture shows the control
    ``excepted`` rather than ``not_met``. Made while two-person review is required (the
    ``governance`` setting) it is ``proposed`` until someone other than the requester approves
    it; otherwise it is ``approved`` at once with ``self_approved`` set.

    A trigger allows only ``proposed`` -> ``approved`` | ``rejected`` | ``revoked`` and
    ``approved`` -> ``revoked``, keeps the request and the decision unchanged, and rejects
    ``DELETE``. User IDs are recorded values, like ``audit_event.actor``.
    """

    __tablename__ = "compliance_exception"
    __table_args__ = (
        Index("ix_compliance_exception_control", "control_id", "state"),
        Index(
            "uq_compliance_exception_proposed",
            "control_id",
            unique=True,
            postgresql_where=text("state = 'proposed'"),
        ),
        one_of("state", COMPLIANCE_EXCEPTION_STATES),
        CheckConstraint("control_id ~ '^[a-z][a-z0-9-]{0,63}$'", name="control_id_format"),
        CheckConstraint(
            f"length(justification) BETWEEN {MIN_STATEMENT} AND {MAX_STATEMENT}",
            name="justification_length",
        ),
        CheckConstraint(
            "expires_on > (requested_at AT TIME ZONE 'UTC')::date AND expires_on <= "
            f"(requested_at AT TIME ZONE 'UTC')::date + {MAX_COMPLIANCE_EXCEPTION_DAYS}",
            name="expiry_bounded",
        ),
        CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name="decided_has_decision",
        ),
        CheckConstraint("state <> 'proposed' OR decided_at IS NULL", name="proposed_undecided"),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name="decision_complete",
        ),
        CheckConstraint(
            "(state = 'revoked') = (revoked_at IS NOT NULL) "
            "AND (revoked_at IS NULL) = (revoked_by IS NULL) "
            "AND (revoked_at IS NULL) = (revoked_by_user_id IS NULL)",
            name="revocation_complete",
        ),
        CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> requested_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint("self_approved = NOT review_required", name="self_approved_unreviewed"),
        CheckConstraint(
            "NOT self_approved OR (decided_by_user_id = requested_by_user_id "
            "AND state IN ('approved', 'revoked'))",
            name="self_approved_by_requester",
        ),
        CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0", name="rejection_explained"
        ),
    )

    id: Mapped[int] = pk()
    # A control in baluarte.compliance.catalogue; not a foreign key, the catalogue is code.
    control_id: Mapped[str] = mapped_column(Text)
    justification: Mapped[str] = mapped_column(Text)
    # The first UTC day it is no longer in force.
    expires_on: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean)
    self_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    requested_by: Mapped[str] = mapped_column(Text)
    requested_by_user_id: Mapped[int] = mapped_column(BigInteger)
    requested_at: Mapped[datetime] = utc_now()
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    revoked_by: Mapped[str | None] = mapped_column(Text)
    revoked_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ComplianceAttestation(Base):
    """An administrator's statement that a control Baluarte cannot observe is in place.

    Current until ``expires_at``; a newer one replaces it. Rows cannot be updated or deleted.
    """

    __tablename__ = "compliance_attestation"
    __table_args__ = (
        Index("ix_compliance_attestation_control", "control_id", "attested_at"),
        CheckConstraint("control_id ~ '^[a-z][a-z0-9-]{0,63}$'", name="control_id_format"),
        CheckConstraint(
            f"length(statement) BETWEEN {MIN_STATEMENT} AND {MAX_STATEMENT}",
            name="statement_length",
        ),
        CheckConstraint("expires_at > attested_at", name="expires_after_attested"),
    )

    id: Mapped[int] = pk()
    control_id: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    attested_by: Mapped[str] = mapped_column(Text)
    attested_by_user_id: Mapped[int] = mapped_column(BigInteger)
    attested_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class LabelTaxonomy(Base):
    """A version of the label taxonomy (``baluarte.labels.taxonomy``), proposed or decided.

    The taxonomy in force is the latest ``approved`` version by ``decided_at``. A version is
    reviewed like a baseline change: while review is required it waits ``proposed`` for someone
    other than its proposer; otherwise it is approved at once by its proposer, ``self_approved``.
    A trigger lets an update only decide a proposed version, and rejects ``DELETE``.
    """

    __tablename__ = "label_taxonomy"
    __table_args__ = (
        UniqueConstraint("instance_id", "version", name="uq_label_taxonomy_version"),
        Index(
            "uq_label_taxonomy_proposed",
            "instance_id",
            unique=True,
            postgresql_where=text("state = 'proposed'"),
        ),
        Index("ix_label_taxonomy_decided", "instance_id", "state", "decided_at"),
        one_of("state", LABEL_TAXONOMY_STATES),
        sha256_hex("yaml_digest"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            f"justification IS NULL OR length(justification) <= {MAX_TAXONOMY_JUSTIFICATION}",
            name="justification_length",
        ),
        CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)", name="decided_unless_proposed"
        ),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name="decision_complete",
        ),
        CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint("review_required OR state = 'approved'", name="unreviewed_is_approved"),
        CheckConstraint(
            "self_approved = NOT review_required", name="self_approved_when_unreviewed"
        ),
        CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0", name="rejection_explained"
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)
    source_yaml: Mapped[str] = mapped_column(Text)
    yaml_digest: Mapped[str] = mapped_column(Text)
    justification: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean)
    self_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    proposed_by: Mapped[str] = mapped_column(Text)
    proposed_by_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    proposed_at: Mapped[datetime] = utc_now()
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)


class LabelPlan(Base):
    """A dry-run label plan (``baluarte.labels.plans``) and, once approved, its application.

    The plan document, its digest and the taxonomy it was built for never change. ``draft`` ->
    ``submitted`` -> ``approved`` or ``rejected``; ``approved`` -> ``applying`` -> ``applied`` or
    ``failed``; ``draft``, ``submitted`` and ``approved`` become ``expired`` after ``expires_at``.
    ``progress`` holds each step's outcome as it is applied. A trigger keeps the plan unchanged
    and rejects ``DELETE``.
    """

    __tablename__ = "label_plan"
    __table_args__ = (
        Index("ix_label_plan_state", "instance_id", "state", "id"),
        one_of("state", LABEL_PLAN_STATES),
        sha256_hex("plan_digest"),
        CheckConstraint(
            f"expires_at > created_at AND expires_at <= created_at + "
            f"interval '{LABEL_PLAN_HOURS} hours'",
            name="expiry_bounded",
        ),
        CheckConstraint(
            f"steps BETWEEN 0 AND {MAX_LABEL_PLAN_STEPS} AND manual >= 0 AND steps + manual >= 1",
            name="steps_bounded",
        ),
        CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> created_by_user_id",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)", name="decision_complete"
        ),
        CheckConstraint(
            "state NOT IN ('approved', 'rejected', 'applying', 'applied', 'failed') "
            "OR decided_at IS NOT NULL",
            name="decided_has_decision",
        ),
        CheckConstraint(
            "state NOT IN ('approved', 'applying', 'applied', 'failed') "
            "OR approved_digest = plan_digest",
            name="approval_binds_digest",
        ),
        CheckConstraint(
            "state NOT IN ('draft', 'submitted', 'rejected') OR approved_digest IS NULL",
            name="unapproved_has_no_digest",
        ),
        CheckConstraint(
            "state NOT IN ('applying', 'applied', 'failed') OR apply_started_at IS NOT NULL",
            name="applying_has_start",
        ),
        CheckConstraint(
            "(state IN ('applied', 'failed', 'expired')) = (finished_at IS NOT NULL)",
            name="finished_has_time",
        ),
        CheckConstraint("(state = 'failed') = (failure IS NOT NULL)", name="failure_explained"),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    taxonomy_id: Mapped[int] = mapped_column(ForeignKey("label_taxonomy.id"))
    state: Mapped[str] = mapped_column(Text, server_default="draft")
    created_by: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The canonical plan document; ``plan_digest`` is the SHA-256 of its canonical JSON.
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB)
    plan_digest: Mapped[str] = mapped_column(Text)
    steps: Mapped[int] = mapped_column(Integer)
    manual: Mapped[int] = mapped_column(Integer)
    review_required: Mapped[bool] = mapped_column(Boolean)
    self_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # The digest the approver reviewed; equal to ``plan_digest`` once approved.
    approved_digest: Mapped[str | None] = mapped_column(Text)
    applied_by: Mapped[str | None] = mapped_column(Text)
    apply_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_step: Mapped[int | None] = mapped_column(Integer)
    failure: Mapped[str | None] = mapped_column(Text)
    # One entry per step reached: its status, the write's HTTP status and the verification.
    progress: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )


class AuditEvent(Base):
    """Administrative audit trail. Rows cannot be updated or deleted, except by the retention
    function ``baluarte_expire_audit`` (migration 0016)."""

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


class AuditShipment(Base):
    """Where audit shipping has got to for one destination (``baluarte.compliance.shipping``).

    ``destination_fingerprint`` is the SHA-256 of the destination's type and address, so a new
    destination starts from the oldest retained event. ``last_shipped_id`` is the highest event
    ID delivered there; delivery is in ID order and at least once.
    """

    __tablename__ = "audit_shipment"
    __table_args__ = (
        sha256_hex("destination_fingerprint"),
        CheckConstraint("last_shipped_id >= 0", name="last_shipped_id_nonnegative"),
        CheckConstraint("consecutive_failures >= 0", name="consecutive_failures_nonnegative"),
    )

    destination_fingerprint: Mapped[str] = mapped_column(Text, primary_key=True)
    # A summary without credentials, e.g. "syslog_tls siem.example.internal:6514".
    destination: Mapped[str] = mapped_column(Text)
    last_shipped_id: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = utc_now()


class BackupRun(Base):
    """One run of the ``baluarte-backup`` service, reported by it. Rows cannot be updated
    (migration 0017); retention deletes them with other evidence."""

    __tablename__ = "backup_run"
    __table_args__ = (
        Index("ix_backup_run_finished", "finished_at"),
        one_of("status", BACKUP_STATUSES),
        sha256_hex("sha256"),
        CheckConstraint("finished_at >= started_at", name="finished_after_started"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_nonnegative"),
        CheckConstraint(
            "status <> 'succeeded' OR (file_name IS NOT NULL AND size_bytes IS NOT NULL "
            "AND sha256 IS NOT NULL AND encrypted)",
            name="success_complete",
        ),
    )

    id: Mapped[int] = pk()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = utc_now()
    status: Mapped[str] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(Text)
    encrypted: Mapped[bool] = mapped_column(Boolean)
    # The age recipient (a public key) the file was encrypted to; not a secret.
    recipient: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    host: Mapped[str | None] = mapped_column(Text)
    reported_by: Mapped[str] = mapped_column(Text, server_default=text("CURRENT_USER"))


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


class StoredSetting(Base):
    """Settings entered through the web UI; one row per section, used when the deployment sets
    none for that section (ADR-0008). Secret values live in ``app_secret``."""

    __tablename__ = "app_setting"
    __table_args__ = (one_of("key", SETTING_KEYS),)

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = utc_now()
    updated_by: Mapped[str] = mapped_column(Text)


class StoredSecret(Base):
    """A secret entered through the web UI, sealed with AES-256-GCM (ADR-0008).

    ``key_id`` names the key that sealed it; the plaintext is never stored.
    """

    __tablename__ = "app_secret"
    __table_args__ = (
        one_of("name", SECRET_NAMES),
        CheckConstraint("key_id ~ '^[0-9a-f]{16}$'", name="key_id_format"),
        CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
        CheckConstraint("octet_length(ciphertext) > 16", name="ciphertext_length"),
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    key_id: Mapped[str] = mapped_column(Text)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = utc_now()
    updated_by: Mapped[str] = mapped_column(Text)


class SetupState(Base):
    """First-run setup: a single row. Only the SHA-256 digest of the setup code is stored.

    Setup completes, once, when the first user with the admin role signs in through the identity
    provider configured during setup; the code is cleared at that point.
    """

    __tablename__ = "setup_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        sha256_hex("code_digest"),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_nonnegative"),
        CheckConstraint(
            "(completed_at IS NULL) = (completed_by_user_id IS NULL)", name="completed_by_whom"
        ),
        CheckConstraint("completed_at IS NULL OR code_digest IS NULL", name="code_cleared"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    code_digest: Mapped[str | None] = mapped_column(Text)
    code_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    failures_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))


class SetupSession(Base):
    """A browser that entered the setup code. Only the SHA-256 digest of the cookie is stored."""

    __tablename__ = "setup_session"
    __table_args__ = (
        sha256_hex("token_digest"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )

    token_digest: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
