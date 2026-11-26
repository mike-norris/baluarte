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
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
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
ROLES = ("admin", "analyst", "auditor", "manager")
RUN_KINDS = (
    "capabilities",
    "groups",
    "projects",
    "security_policies",
    "changes",
    "labels",
    "time",
    "issues",
)
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
PLAN_STATES = (
    "draft",
    "submitted",
    "approved",
    "rejected",
    "expired",
    "applying",
    "applied",
    "failed",
)
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
    "time_tracking",
    "notifications",
    "remediation_write_token",
    "template_write_token",
)
SECRET_NAMES = (
    "oidc_client_secret",
    "gitlab_token",
    "audit_shipping_es_api_key",
    "audit_shipping_es_password",
    "label_write_token",
    "notifications_smtp_password",
    "notifications_webhook_url",
    "remediation_write_token",
    "template_write_token",
)
NOTIFICATION_EVENTS = (
    "new_findings",
    "collection_unhealthy",
    "collection_recovered",
    "review_waiting",
    "weekly_digest",
)
NOTIFICATION_CHANNELS = ("email", "webhook")
DELIVERY_STATUSES = ("pending", "sent", "failed")
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
# An issue plan (ADR-0011) is valid this long, and holds at most this many steps.
ISSUE_PLAN_HOURS = 24
MAX_ISSUE_PLAN_STEPS = 5000
CLOSED_ISSUE_COMMITS = ("merge_request", "mention", "none", "unknown", "not_checked")
# The auditable issue register (roadmap M12, ADR-0015).
REGISTER_CATEGORIES = ("engineering", "security_finding", "exception", "remediation")
REGISTER_DECISIONS = ("include", "exclude")
MIN_REGISTER_REASON = 20
MAX_REGISTER_REASON = 2000
ISSUE_EVIDENCE_STATUSES = ("complete", "partial", "failed")
ISSUE_MAPPING_STATES = ("proposed", "active", "rejected", "withdrawn")
ISSUE_EVIDENCE_KINDS = (
    "description",
    "note",
    "event",
    "link",
    "merge_request",
    "pipeline",
    "release",
    "attachment",
)
MIN_MAPPING_RATIONALE = 20
MAX_MAPPING_RATIONALE = 2000
MIN_ITEM_RATIONALE = 10
MAX_ITEM_RATIONALE = 1000
MAX_PERIOD_NAME = 120
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


class Workspace(Base):
    """The isolation boundary: an organization's connections and everything built on them
    (ADR-0013)."""

    __tablename__ = "workspace"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name="slug_format"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint(
            "group_roles IS NULL OR jsonb_typeof(group_roles) = 'object'",
            name="group_roles_object",
        ),
    )

    id: Mapped[int] = pk()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    # Identity provider groups per role, {"admin": ["group", ...]}; NULL for the default
    # workspace, which uses the sign-in settings' mapping.
    group_roles: Mapped[dict[str, list[str]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = utc_now()


PROVIDERS = ("gitlab", "github")
AUTH_KINDS = ("token", "app")
PROVIDER_STATES = ("active", "missing")
MEMBERSHIP_SUBJECTS = ("organization", "team")


class GitLabInstance(Base):
    """A provider connection in a workspace (ADR-0013). Named for its first provider; GitHub
    connections follow in roadmap M13."""

    __tablename__ = "gitlab_instance"
    __table_args__ = (
        one_of("provider", PROVIDERS),
        one_of("auth_kind", AUTH_KINDS),
        CheckConstraint("auth_kind = 'token' OR provider = 'github'", name="app_for_github"),
        CheckConstraint(
            "(auth_kind = 'app') = (app_id IS NOT NULL AND installation_id IS NOT NULL)",
            name="app_identified",
        ),
        CheckConstraint("name IS NULL OR char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 120", name="timeout_range"),
        CheckConstraint(
            "sync_interval_minutes = 0 OR sync_interval_minutes BETWEEN 15 AND 10080",
            name="interval_range",
        ),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    provider: Mapped[str] = mapped_column(Text, server_default="gitlab")
    # A display name; the URL is shown when it is not set.
    name: Mapped[str | None] = mapped_column(Text)
    # A paused connection is not collected; its data stays.
    paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Settings for connections added on the Connections page; the connection configured in the
    # deployment or on the Settings page uses those settings instead.
    timeout_seconds: Mapped[float] = mapped_column(Float, server_default=text("15"))
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, server_default=text("360"))
    # Whether the URL may resolve to a private network address (a self-managed host).
    allow_private_network: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # How a connection authenticates: a token (both providers) or, for GitHub, an App
    # installation (``app_id`` and ``installation_id``; the private key is sealed like a token).
    auth_kind: Mapped[str] = mapped_column(Text, server_default="token")
    app_id: Mapped[int | None] = mapped_column(BigInteger)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    # A removed connection is no longer collected; its data and evidence stay.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    base_url: Mapped[str] = mapped_column(Text, unique=True)
    version: Mapped[str | None] = mapped_column(Text)
    edition: Mapped[str | None] = mapped_column(Text)
    capability_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_now()


class ProviderTeam(Base):
    """A team in a connection's organization (GitHub; roadmap M13.2), upserted on its provider ID.
    A team no longer listed is ``missing``; its row and history stay."""

    __tablename__ = "provider_team"
    __table_args__ = (
        UniqueConstraint("instance_id", "provider_id"),
        one_of("state", PROVIDER_STATES),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    provider_id: Mapped[int] = mapped_column(BigInteger)
    parent_provider_id: Mapped[int | None] = mapped_column(BigInteger)
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    privacy: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="active")
    first_seen_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()


WEBHOOK_STATUSES = ("queued", "processed", "ignored", "failed")


class WebhookDelivery(Base):
    """A webhook delivery a connection accepted (GitHub; roadmap M13.4b), once per delivery ID.

    ``queued`` until a read covers it, then ``processed``; ``ignored`` for an event Baluarte does
    not act on; ``failed`` when its read failed. The body is never stored, only its SHA-256.
    """

    __tablename__ = "webhook_delivery"
    __table_args__ = (
        UniqueConstraint("instance_id", "delivery_id"),
        one_of("status", WEBHOOK_STATUSES),
        CheckConstraint("char_length(delivery_id) BETWEEN 1 AND 100", name="delivery_id_length"),
        sha256_hex("body_sha256"),
        Index(
            "ix_webhook_delivery_pending",
            "instance_id",
            "repository_id",
            "received_at",
            postgresql_where=text("status = 'queued'"),
        ),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    delivery_id: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    repository_id: Mapped[int | None] = mapped_column(BigInteger)
    body_sha256: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = utc_now()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ProviderMembership(Base):
    """Who belongs to the organization (``subject_kind`` ``organization``) or a team, and their
    role there, as last listed (roadmap M13.2). Someone no longer listed is ``missing``."""

    __tablename__ = "provider_membership"
    __table_args__ = (
        UniqueConstraint(
            "instance_id",
            "subject_kind",
            "subject_provider_id",
            "user_provider_id",
            name="uq_provider_membership_member",
        ),
        Index("ix_provider_membership_login", "instance_id", "login"),
        one_of("subject_kind", MEMBERSHIP_SUBJECTS),
        one_of("state", PROVIDER_STATES),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    subject_kind: Mapped[str] = mapped_column(Text)
    subject_provider_id: Mapped[int] = mapped_column(BigInteger)
    user_provider_id: Mapped[int] = mapped_column(BigInteger)
    login: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="active")
    first_seen_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()


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
    # Sorted, without duplicates; None until a discovery has read them (migration 0047).
    topics: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
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
        UniqueConstraint("workspace_id", "name", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        sha256_hex("yaml_digest"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    source_yaml: Mapped[str] = mapped_column(Text)
    yaml_digest: Mapped[str] = mapped_column(Text)
    effective_at: Mapped[datetime] = utc_now()
    created_at: Mapped[datetime] = utc_now()


class ConfigurationTemplate(Base):
    """An immutable configuration template version (roadmap M14.1, ADR-0016); a trigger refuses
    any change or deletion. ``extends_id`` is the exact parent version it extends."""

    __tablename__ = "configuration_template"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        sha256_hex("yaml_digest"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    source_yaml: Mapped[str] = mapped_column(Text)
    yaml_digest: Mapped[str] = mapped_column(Text)
    extends_id: Mapped[int | None] = mapped_column(ForeignKey("configuration_template.id"))
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()


class TemplateAssignment(Base):
    """One template version pinned to a connection, group or project (roadmap M14.1).

    ``target_id`` is the provider ID of a group or project, or the connection's row ID, as for
    ``PolicyAssignment``. A target holds at most one template; ``overrides`` replace settings for
    it and need ``override_reason``.
    """

    __tablename__ = "template_assignment"
    __table_args__ = (
        UniqueConstraint("instance_id", "target_type", "target_id"),
        Index("ix_template_assignment_template_id", "template_id"),
        one_of("target_type", TARGET_TYPES),
        CheckConstraint(
            "target_type <> 'instance' OR target_id = instance_id", name="instance_target"
        ),
        CheckConstraint(
            "(overrides IS NULL) = (override_reason IS NULL) AND (override_reason IS NULL "
            "OR char_length(override_reason) BETWEEN 20 AND 2000)",
            name="override_reason",
        ),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    template_id: Mapped[int] = mapped_column(ForeignKey("configuration_template.id"))
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[int] = mapped_column(BigInteger)
    overrides: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    override_reason: Mapped[str | None] = mapped_column(Text)
    # Propose (never apply) a change set fixing drift of the settings this assignment decides.
    auto_remediate: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    assigned_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()


RELEASE_PRESETS = ("merge_request", "release_tag", "release_branch", "custom")


class ReleasePractice(Base):
    """How projects are meant to reach production (release governance RG.2, ADR-0017).

    ``settings`` holds the practice's parameters, validated by ``baluarte.releases.practices``.
    Unlike templates, a practice is edited in place; every change is audited with its values.
    """

    __tablename__ = "release_practice"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        one_of("preset", RELEASE_PRESETS),
        CheckConstraint("char_length(name) BETWEEN 1 AND 63", name="name_length"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    preset: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    updated_by: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = utc_now()


class ReleasePracticeAssignment(Base):
    """One release practice for a connection, group or project; the nearest on a project's path
    decides. ``target_id`` is as for ``TemplateAssignment``."""

    __tablename__ = "release_practice_assignment"
    __table_args__ = (
        UniqueConstraint("instance_id", "target_type", "target_id"),
        Index("ix_release_practice_assignment_practice_id", "practice_id"),
        one_of("target_type", TARGET_TYPES),
        CheckConstraint(
            "target_type <> 'instance' OR target_id = instance_id", name="instance_target"
        ),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    practice_id: Mapped[int] = mapped_column(ForeignKey("release_practice.id"))
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[int] = mapped_column(BigInteger)
    assigned_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()


class TemplateException(Base):
    """A time-limited waiver of one template setting for one project, approved by someone else
    (roadmap M14.2). The same lifecycle and trigger rules as ``PolicyException``; ``setting`` is
    a template setting key such as ``merge_requests.min_approvals``."""

    __tablename__ = "template_exception"
    __table_args__ = (
        Index(
            "uq_template_exception_active",
            "project_id",
            "setting",
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
            "decided_by_user_id IS NULL OR decided_by_user_id <> requested_by_user_id "
            "OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = requested_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), index=True)
    setting: Mapped[str] = mapped_column(Text)
    justification: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="requested")
    requested_by_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    requested_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    ended_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_note: Mapped[str | None] = mapped_column(Text)


CHANGE_SET_STATES = (
    "proposed",
    "approved",
    "rejected",
    "withdrawn",
    "expired",
    "applying",
    "paused",
    "cancelled",
    "finished",
    "rolling_back",
    "rolled_back",
)
CHANGE_ITEM_STATUSES = (
    "pending",
    "writing",
    "applied",
    "skipped",
    "out_of_date",
    "manual",
    "failed",
    "cancelled",
)
ROLLBACK_STATUSES = ("rolled_back", "not_rolled_back", "failed")
MAX_CHANGE_SET_HOURS = 7 * 24


class TemplateChangeSet(Base):
    """Exact template changes to selected projects, with before and after values from evidence
    (roadmap M14.3, ``baluarte.templates.change_sets``).

    The document, its digest and its counts never change. ``proposed`` becomes ``approved`` or
    ``rejected`` by someone other than the creator; a ``sensitive`` set (one that weakens
    protection or removes access) needs a first approval and then a second by a different
    person. The creator may withdraw a proposed set; ``proposed`` and ``approved`` sets expire at
    ``expires_at``. Approval records the digest reviewed, which must equal the set's. A trigger
    (migration 0040) enforces the transitions, keeps recorded decisions and rejects ``DELETE``.
    """

    __tablename__ = "template_change_set"
    __table_args__ = (
        Index("ix_template_change_set_state", "instance_id", "state", "id"),
        one_of("state", CHANGE_SET_STATES),
        sha256_hex("digest"),
        CheckConstraint("length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint("targets >= 1 AND changes >= 1 AND skipped >= 0", name="counts"),
        CheckConstraint(
            f"expires_at > created_at AND expires_at <= created_at + "
            f"interval '{MAX_CHANGE_SET_HOURS} hours'",
            name="expiry_bounded",
        ),
        CheckConstraint(
            "(first_approved_at IS NULL) = (first_approved_by_user_id IS NULL)",
            name="first_approval_complete",
        ),
        CheckConstraint(
            "sensitive OR first_approved_at IS NULL", name="first_approval_when_sensitive"
        ),
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)", name="decision_complete"
        ),
        CheckConstraint(
            "state IN ('proposed', 'withdrawn') OR decided_at IS NOT NULL "
            "OR (state = 'expired' AND approved_digest IS NULL)",
            name="decided_has_decision",
        ),
        CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0", name="rejection_explained"
        ),
        CheckConstraint(
            "state IN ('proposed', 'rejected', 'withdrawn', 'expired') "
            "OR approved_digest IS NOT NULL",
            name="approved_has_digest",
        ),
        CheckConstraint(
            "state IN ('proposed', 'approved', 'rejected', 'withdrawn', 'expired') "
            "OR (apply_started_at IS NOT NULL AND applied_by_user_id IS NOT NULL "
            "AND stage_targets >= 1 AND runs >= 1)",
            name="applying_has_start",
        ),
        CheckConstraint("stage_targets BETWEEN 0 AND targets", name="stage_bounded"),
        CheckConstraint(
            "(state IN ('finished', 'rolled_back')) = (finished_at IS NOT NULL) "
            "OR state = 'rolling_back'",
            name="finished_has_time",
        ),
        CheckConstraint(
            "(cancelled_at IS NULL) = (cancelled_by_user_id IS NULL) "
            "AND (state <> 'cancelled' OR cancelled_at IS NOT NULL)",
            name="cancelled_has_user",
        ),
        CheckConstraint(
            "(state IN ('rolling_back', 'rolled_back')) = (rollback_started_at IS NOT NULL) "
            "AND (rollback_started_at IS NULL) = (rollback_by_user_id IS NULL)",
            name="rollback_has_start",
        ),
        CheckConstraint(
            "state NOT IN ('proposed', 'rejected', 'withdrawn') OR approved_digest IS NULL",
            name="unapproved_has_no_digest",
        ),
        CheckConstraint(
            "approved_digest IS NULL OR approved_digest = digest", name="approval_binds_digest"
        ),
        CheckConstraint(
            "NOT sensitive OR state <> 'approved' OR first_approved_at IS NOT NULL",
            name="sensitive_approved_twice",
        ),
        CheckConstraint(
            "development_decision OR ("
            "(decided_by_user_id IS NULL OR decided_by_user_id <> created_by_user_id) "
            "AND (first_approved_by_user_id IS NULL "
            "OR (first_approved_by_user_id <> created_by_user_id "
            "AND first_approved_by_user_id IS DISTINCT FROM decided_by_user_id)))",
            name="decided_by_others",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = created_by_user_id "
            "OR first_approved_by_user_id = created_by_user_id",
            name="development_decision_by_proposer",
        ),
        CheckConstraint(
            "(state IN ('withdrawn', 'expired')) = (ended_at IS NOT NULL)", name="ended_has_time"
        ),
        CheckConstraint(
            "(created_by_user_id IS NULL) = created_by_system", name="system_or_user_creator"
        ),
        CheckConstraint(
            "NOT (created_by_system AND development_decision)",
            name="system_sets_not_development",
        ),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="proposed")
    # The canonical document; ``digest`` is the SHA-256 of its canonical JSON.
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(Text)
    targets: Mapped[int] = mapped_column(Integer)
    changes: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[int] = mapped_column(Integer)
    sensitive: Mapped[bool] = mapped_column(Boolean)
    # None for a set Baluarte proposed itself (automatic remediation, created_by_system).
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_by_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    first_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_approval_note: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    approved_digest: Mapped[str | None] = mapped_column(Text)
    # Development mode only: the creator gave an approval (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Applying (M14.4): who started it, how many targets the stages allow so far, and how many
    # runs (start, continue, rollback) have been queued.
    applied_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    apply_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage_targets: Mapped[int] = mapped_column(Integer, server_default="0")
    runs: Mapped[int] = mapped_column(Integer, server_default="0")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    rollback_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why the last run stopped before its stage was done (writes switched off, no token).
    halt_reason: Mapped[str | None] = mapped_column(Text)


class TemplateChangeItem(Base):
    """One change of a template change set and its progress (roadmap M14.4).

    What was approved (project, setting, ``before``, ``after``, ``irreversible``) never changes;
    a trigger (migration 0041) enforces that and rejects ``DELETE``. ``previous`` is the value
    read just before writing, kept for rollback; ``observed`` is what a read showed afterwards.
    """

    __tablename__ = "template_change_item"
    __table_args__ = (
        UniqueConstraint("change_set_id", "position"),
        one_of("status", CHANGE_ITEM_STATUSES),
        CheckConstraint(
            "rollback_status IS NULL OR rollback_status IN "
            "('rolled_back', 'not_rolled_back', 'failed')",
            name="rollback_status_allowed",
        ),
        CheckConstraint(
            "position >= 0 AND target_index >= 0 AND attempts >= 0", name="non_negative"
        ),
        CheckConstraint(
            "(status IN ('pending', 'writing')) = (finished_at IS NULL)", name="finished_has_time"
        ),
        CheckConstraint(
            "(rollback_status IS NULL) = (rolled_back_at IS NULL)", name="rollback_has_time"
        ),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    change_set_id: Mapped[int] = mapped_column(ForeignKey("template_change_set.id"))
    position: Mapped[int] = mapped_column(Integer)
    # The target's place in the set's document; stages run targets in this order.
    target_index: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    project_gitlab_id: Mapped[int] = mapped_column(BigInteger)
    project_path: Mapped[str] = mapped_column(Text)
    setting: Mapped[str] = mapped_column(Text)
    before: Mapped[Any] = mapped_column(JSONB)
    after: Mapped[Any] = mapped_column(JSONB)
    # Why this change cannot be fully undone, disclosed before applying; None when it can.
    irreversible: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    request_status: Mapped[int | None] = mapped_column(Integer)
    previous: Mapped[Any | None] = mapped_column(JSONB)
    observed: Mapped[Any | None] = mapped_column(JSONB)
    message: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_status: Mapped[str | None] = mapped_column(Text)
    rollback_message: Mapped[str | None] = mapped_column(Text)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SavedSelection(Base):
    """Named target criteria for change sets (``baluarte.templates.selection``)."""

    __tablename__ = "saved_selection"
    __table_args__ = (
        UniqueConstraint("instance_id", "name"),
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    name: Mapped[str] = mapped_column(Text)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
        # Trends count each run's results by outcome and severity for a set of projects.
        Index(
            "ix_evaluation_run_outcome", "evaluation_run_id", "outcome", "severity", "project_id"
        ),
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
            "decided_by_user_id IS NULL OR decided_by_user_id <> requested_by_user_id "
            "OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = requested_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
            "workspace_id",
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
            "OR decided_by_user_id <> proposed_by_user_id OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = proposed_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
            "OR decided_by_user_id <> proposed_by_user_id OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = proposed_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
            "OR decided_by_user_id <> proposed_by_user_id OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = proposed_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
        one_of("review_outcome", CHANGE_OUTCOMES),
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
    # The review check alone (approvals, no direct push), for review rates; null for a
    # deployment, or for a change recorded before it was kept.
    review_outcome: Mapped[str | None] = mapped_column(Text)
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
            "OR decided_by_user_id <> requested_by_user_id OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = requested_by_user_id",
            name="development_decision_by_proposer",
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
    """A remediation plan (BAL-031, BAL-032): the GitLab changes that would fix open findings.

    The plan document and its digest never change. ``draft`` becomes ``submitted`` (by its
    creator), then ``approved`` or ``rejected`` by someone else; ``draft``, ``submitted`` or
    ``approved`` becomes ``expired`` once ``expires_at`` has passed. Approval records the digest
    the approver reviewed, which must equal the plan's. An approved plan becomes ``applying``
    when someone applies it before it expires, then ``applied`` or ``failed``; ``progress``
    holds each step's outcome. A trigger enforces the transitions, keeps the plan and the
    decision unchanged and rejects ``DELETE``.
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
            "decided_by_user_id IS NULL OR decided_by_user_id <> created_by_user_id "
            "OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = created_by_user_id",
            name="development_decision_by_proposer",
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
            "state NOT IN ('submitted', 'approved', 'rejected', 'applying', 'applied', 'failed') "
            "OR submitted_at IS NOT NULL",
            name="submitted_has_time",
        ),
        CheckConstraint(
            "state NOT IN ('approved', 'applying', 'applied', 'failed') "
            "OR approved_digest IS NOT NULL",
            name="approved_has_digest",
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
        CheckConstraint(
            "state NOT IN ('applying', 'applied', 'failed') "
            "OR (apply_started_at IS NOT NULL AND applied_by_user_id IS NOT NULL)",
            name="applying_has_start",
        ),
        CheckConstraint(
            "(state IN ('applied', 'failed')) = (finished_at IS NOT NULL)",
            name="finished_has_time",
        ),
        CheckConstraint("(state = 'failed') = (failure IS NOT NULL)", name="failure_explained"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # The digest the approver reviewed; equal to ``plan_digest`` when approved.
    approved_digest: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    apply_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_step: Mapped[int | None] = mapped_column(Integer)
    failure: Mapped[str | None] = mapped_column(Text)
    # One entry per step: its status, the write's HTTP status and the verification.
    progress: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )


class LabelTaxonomy(Base):
    """A version of the label taxonomy (``baluarte.labels.taxonomy``), proposed or decided.

    The taxonomy in force is the latest ``approved`` version by ``decided_at``. A version is
    reviewed like a baseline change: while review is required it waits ``proposed`` for someone
    other than its proposer; otherwise it is approved at once by its proposer, ``self_approved``.
    A trigger lets an update only decide a proposed version, and rejects ``DELETE``.
    """

    __tablename__ = "label_taxonomy"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_label_taxonomy_version"),
        Index(
            "uq_label_taxonomy_proposed",
            "workspace_id",
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
            "OR decided_by_user_id <> proposed_by_user_id OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = proposed_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
            "OR decided_by_user_id <> created_by_user_id OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = created_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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


class ClosedIssue(Base):
    """A closed issue as last read, for the time rules on closed issues (ADR-0011).

    ``commits`` says what links the issue to code: ``merge_request`` (a merge request closes it
    or is linked to it), ``mention`` (a commit or merge request mentions it in a system note),
    ``none``, ``unknown`` while that could not be read, or ``not_checked`` when no rule needs it
    (time was logged). ``commits_for`` is the
    ``issue_updated_at`` it was determined for, so it is checked again after the issue changes.
    A reopened issue's row is removed.
    """

    __tablename__ = "closed_issue"
    __table_args__ = (
        Index("ix_closed_issue_instance", "instance_id"),
        Index("ix_closed_issue_observation", "observation_id"),
        one_of("commits", CLOSED_ISSUE_COMMITS),
    )

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    issue_iid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    issue_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    web_url: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    estimate_seconds: Mapped[int] = mapped_column(Integer)
    spent_seconds: Mapped[int] = mapped_column(Integer)
    merge_requests_count: Mapped[int] = mapped_column(Integer)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issue_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commits: Mapped[str] = mapped_column(Text)
    commits_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    read_at: Mapped[datetime] = utc_now()


class RegisterIssue(Base):
    """A project issue as last read, for the auditable issue register (ADR-0015).

    Every issue type GitLab lists (issues, incidents, tasks, test cases) is kept, in any state, for
    the projects of workspaces that have a register. ``opened_at``, ``source_updated_at`` and
    ``closed_at`` are GitLab's timestamps; ``read_at`` is when Baluarte read it, and
    ``observation_id`` the read that recorded it.
    """

    __tablename__ = "register_issue"
    __table_args__ = (
        Index("ix_register_issue_instance", "instance_id"),
        Index("ix_register_issue_observation", "observation_id"),
        Index("ix_register_issue_labels", "labels", postgresql_using="gin"),
    )

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    issue_iid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    gitlab_id: Mapped[int | None] = mapped_column(BigInteger)
    issue_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    author: Mapped[str | None] = mapped_column(Text)
    assignees: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    milestone: Mapped[str | None] = mapped_column(Text)
    web_url: Mapped[str | None] = mapped_column(Text)
    confidential: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    read_at: Mapped[datetime] = utc_now()


class IssueEvidence(Base):
    """The latest evidence capture of an auditable issue (roadmap M12.2, ADR-0015).

    ``observation_id`` is the capture itself: one observation holding the issue, its comments,
    history, links, merge requests, releases and attachments, with a status per source.
    ``captured_for`` is the issue's ``source_updated_at`` it was taken for; ``status`` is
    ``complete`` when every source was read, ``partial`` when some were not (``gaps`` says which),
    and ``failed`` when the issue itself could not be read.
    """

    __tablename__ = "issue_evidence"
    __table_args__ = (
        Index("ix_issue_evidence_instance", "instance_id"),
        Index("ix_issue_evidence_observation", "observation_id"),
        one_of("status", ISSUE_EVIDENCE_STATUSES),
    )

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    issue_iid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    status: Mapped[str] = mapped_column(Text)
    gaps: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    captured_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = utc_now()


class CapturedFile(Base):
    """A file captured as evidence, stored once by its SHA-256 however many records cite it.

    Rows are never updated or deleted by the application. The content type is the one GitLab
    sent, kept for the record; files are always served as downloads.
    """

    __tablename__ = "captured_file"
    __table_args__ = (
        sha256_hex("sha256"),
        CheckConstraint("size_bytes = octet_length(content)", name="size_matches"),
    )

    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(Text)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    captured_at: Mapped[datetime] = utc_now()


class IssueControlMapping(Base):
    """An auditable issue mapped to a framework control (roadmap M12.3,
    ``baluarte.register.mapping``), with a rationale and the capture it rests on.

    Review follows the ``governance`` setting like ``control_mapping_change``: ``proposed`` until
    someone other than the proposer approves it (``active``) or rejects it, or ``active`` at once
    with ``self_approved``. A revision supersedes an active mapping, which is ``withdrawn`` when
    the revision takes effect; an active mapping may also be withdrawn with a reason. A trigger
    allows only those transitions, changes nothing that was proposed, and rejects ``DELETE``.
    """

    __tablename__ = "issue_control_mapping"
    __table_args__ = (
        Index(
            "uq_issue_control_mapping_open",
            "workspace_id",
            "project_id",
            "issue_iid",
            "framework",
            "control_id",
            unique=True,
            postgresql_where=text("state IN ('proposed', 'active') AND supersedes_id IS NULL"),
        ),
        Index(
            "uq_issue_control_mapping_revision",
            "supersedes_id",
            unique=True,
            postgresql_where=text("state IN ('proposed', 'active') AND supersedes_id IS NOT NULL"),
        ),
        Index("ix_issue_control_mapping_issue", "project_id", "issue_iid"),
        Index("ix_issue_control_mapping_control", "framework", "control_id"),
        Index("ix_issue_control_mapping_workspace", "workspace_id", "state"),
        one_of("state", ISSUE_MAPPING_STATES),
        one_of("framework", MAPPING_FRAMEWORKS),
        CheckConstraint(
            f"length(rationale) BETWEEN {MIN_MAPPING_RATIONALE} AND {MAX_MAPPING_RATIONALE}",
            name="rationale_length",
        ),
        CheckConstraint(
            "(state = 'withdrawn') = (withdrawn_at IS NOT NULL)", name="withdrawn_when_ended"
        ),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    issue_iid: Mapped[int] = mapped_column(BigInteger)
    framework: Mapped[str] = mapped_column(Text)
    control_id: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("issue_control_mapping.id"))
    state: Mapped[str] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean)
    self_approved: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    mapping_version: Mapped[str] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(Text)
    proposed_by_user_id: Mapped[int] = mapped_column(BigInteger)
    proposed_at: Mapped[datetime] = utc_now()
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    withdrawn_by: Mapped[str | None] = mapped_column(Text)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdraw_reason: Mapped[str | None] = mapped_column(Text)


class IssueControlEvidence(Base):
    """One captured item a mapping cites, with its own rationale. ``item_key`` names it in the
    pinned capture; its kind, label and the time it happened are copied from the capture, so
    the mapping reads the same whatever is captured later. Rows are written with their mapping
    and never changed or deleted (a trigger)."""

    __tablename__ = "issue_control_evidence"
    __table_args__ = (
        one_of("item_kind", ISSUE_EVIDENCE_KINDS),
        CheckConstraint(
            f"length(rationale) BETWEEN {MIN_ITEM_RATIONALE} AND {MAX_ITEM_RATIONALE}",
            name="rationale_length",
        ),
        UniqueConstraint("mapping_id", "item_key", name="uq_issue_control_evidence_item"),
    )

    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("issue_control_mapping.id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_key: Mapped[str] = mapped_column(Text)
    item_kind: Mapped[str] = mapped_column(Text)
    item_label: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str] = mapped_column(Text)


class AuditPeriod(Base):
    """A named audit period: evidence dated inside it is kept apart from later remediation.
    Periods may overlap (one per framework, say). Ended, never edited or deleted (a trigger)."""

    __tablename__ = "audit_period"
    __table_args__ = (
        Index("ix_audit_period_workspace", "workspace_id"),
        CheckConstraint("ends_on >= starts_on", name="ends_after_start"),
        CheckConstraint(f"length(name) BETWEEN 1 AND {MAX_PERIOD_NAME}", name="name_length"),
        CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)", name="ended_together"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"))
    name: Mapped[str] = mapped_column(Text)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RegisterCursor(Base):
    """How far a project's issues have been read: ``read_through`` is GitLab's ``updated_at`` of
    the latest change seen, so the next read lists only what changed since."""

    __tablename__ = "register_cursor"

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    read_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime] = utc_now()


class RegisterRule(Base):
    """A selection rule: issues matching all its non-empty criteria are auditable.

    ``labels`` matches an issue with any of them, ``project_ids`` (``gitlab_project.id``) one of
    the projects, ``issue_types`` one of the types. Rules are not edited: one is ended and another
    created, and a trigger allows only setting ``ended_at`` and ``ended_by`` once, and rejects
    ``DELETE``.
    """

    __tablename__ = "register_rule"
    __table_args__ = (
        Index(
            "uq_register_rule_active_name",
            "workspace_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        one_of("category", REGISTER_CATEGORIES),
        CheckConstraint(
            "cardinality(labels) + cardinality(project_ids) + cardinality(issue_types) > 0",
            name="has_criteria",
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)", name="ended_together"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"))
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    project_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=text("'{}'::bigint[]")
    )
    issue_types: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RegisterDecision(Base):
    """An explicit decision on one issue: ``include`` it, whatever the rules say, or ``exclude``
    one the rules select. Both need a reason; an excluded issue stays listed with it, so nothing
    leaves the register unseen. One open decision per issue; it is ended, not edited or deleted
    (enforced by a trigger, like ``register_rule``)."""

    __tablename__ = "register_decision"
    __table_args__ = (
        Index(
            "uq_register_decision_open",
            "project_id",
            "issue_iid",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_register_decision_workspace", "workspace_id"),
        one_of("decision", REGISTER_DECISIONS),
        CheckConstraint(
            "category IS NULL OR (decision = 'include' AND category IN "
            + "("
            + ", ".join(f"'{value}'" for value in REGISTER_CATEGORIES)
            + "))",
            name="category_for_inclusions",
        ),
        CheckConstraint(
            f"length(reason) BETWEEN {MIN_REGISTER_REASON} AND {MAX_REGISTER_REASON}",
            name="reason_length",
        ),
        CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)", name="ended_together"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    issue_iid: Mapped[int] = mapped_column(BigInteger)
    decision: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = utc_now()
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IssuePlan(Base):
    """A dry-run issue plan (``baluarte.timetracking.issue_plans``, ADR-0011): labels and time
    estimates to set on closed issues, and once approved, its application.

    The plan document and its digest never change. ``draft`` ->
    ``submitted`` -> ``approved`` or ``rejected``; ``approved`` -> ``applying`` -> ``applied`` or
    ``failed``; ``draft``, ``submitted`` and ``approved`` become ``expired`` after ``expires_at``.
    ``progress`` holds each step's outcome as it is applied. A trigger keeps the plan unchanged
    and rejects ``DELETE``.
    """

    __tablename__ = "issue_plan"
    __table_args__ = (
        Index("ix_issue_plan_state", "instance_id", "state", "id"),
        one_of("state", LABEL_PLAN_STATES),
        sha256_hex("plan_digest"),
        CheckConstraint(
            f"expires_at > created_at AND expires_at <= created_at + "
            f"interval '{ISSUE_PLAN_HOURS} hours'",
            name="expiry_bounded",
        ),
        CheckConstraint(
            f"steps BETWEEN 0 AND {MAX_ISSUE_PLAN_STEPS} AND manual >= 0 AND steps + manual >= 1",
            name="steps_bounded",
        ),
        CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> created_by_user_id OR development_decision",
            name="decided_by_another_user",
        ),
        CheckConstraint(
            "NOT development_decision OR decided_by_user_id = created_by_user_id",
            name="development_decision_by_proposer",
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
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
    # Development mode only: the proposer decided their own item (baluarte.settings).
    development_decision: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
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
    # The workspace the request or job acted in; NULL for events before migration 0032 and for
    # actions outside any workspace (sign-in, deployment settings).
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspace.id"), index=True)


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


class Notification(Base):
    """A message produced by one event (``baluarte.notifications``), stored as it is sent.

    ``dedup_key`` names the event, e.g. ``findings:<run id>`` or ``digest:<ISO week>``; it is
    unique, so an event produces at most one notification however many workers notice it.
    Messages hold no personal data: counts, project paths, rule IDs and links only.
    """

    __tablename__ = "notification"
    __table_args__ = (
        Index("ix_notification_event_type", "event_type", "id"),
        Index("ix_notification_created", "created_at"),
        one_of("event_type", NOTIFICATION_EVENTS),
        sha256_hex("body_digest"),
        CheckConstraint("length(dedup_key) BETWEEN 1 AND 200", name="dedup_key_length"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    event_type: Mapped[str] = mapped_column(Text)
    dedup_key: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = utc_now()
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    # SHA-256 of the subject and the plain text body.
    body_digest: Mapped[str] = mapped_column(Text)


class NotificationDelivery(Base):
    """One notification sent to one channel: ``pending`` until sent, retried with backoff, then
    ``sent`` or ``failed``. ``last_error`` is redacted and never holds a secret."""

    __tablename__ = "notification_delivery"
    __table_args__ = (
        UniqueConstraint("notification_id", "channel"),
        Index(
            "ix_notification_delivery_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        one_of("channel", NOTIFICATION_CHANNELS),
        one_of("status", DELIVERY_STATUSES),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint(
            "(status = 'pending') = (next_attempt_at IS NOT NULL)",
            name="pending_has_next_attempt",
        ),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="sent_has_time"),
    )

    id: Mapped[int] = pk()
    notification_id: Mapped[int] = mapped_column(ForeignKey("notification.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint(
            "offsite_status IS NULL OR offsite_status IN ('copied', 'failed')",
            name="offsite_status_allowed",
        ),
        CheckConstraint(
            "offsite_status IS NULL OR (offsite_location IS NOT NULL AND "
            "(offsite_status = 'copied') = (offsite_error IS NULL))",
            name="offsite_complete",
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
    # The off-site copy (migration 0042): None when no off-site target is configured, else
    # copied (uploaded and its size and checksum read back) or failed, where to
    # (s3://bucket/key at the endpoint's host, never credentials) and why it failed.
    offsite_status: Mapped[str | None] = mapped_column(Text)
    offsite_location: Mapped[str | None] = mapped_column(Text)
    offsite_error: Mapped[str | None] = mapped_column(Text)


class RestoreRun(Base):
    """One restore test reported by the ``baluarte-backup`` service (migration 0043): a backup
    fetched, checked against its recorded SHA-256, decrypted into a temporary PostgreSQL and
    checked. Rows cannot be updated. ``backup_run_id`` is not a foreign key, since retention
    removes old backup runs while their restore runs stay as evidence."""

    __tablename__ = "restore_run"
    __table_args__ = (
        Index("ix_restore_run_finished", "finished_at"),
        one_of("status", BACKUP_STATUSES),
        CheckConstraint("source IS NULL OR source IN ('offsite', 'local')", name="source_allowed"),
        sha256_hex("sha256"),
        CheckConstraint("finished_at >= started_at", name="finished_after_started"),
        CheckConstraint(
            "coalesce(table_count, 0) >= 0 AND coalesce(audit_events, 0) >= 0 "
            "AND coalesce(digest_mismatches, 0) >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR (file_name IS NOT NULL AND sha256 IS NOT NULL "
            "AND source IS NOT NULL AND schema_revision IS NOT NULL AND table_count > 0 "
            "AND audit_events IS NOT NULL AND digest_mismatches = 0 AND error IS NULL)",
            name="success_complete",
        ),
        CheckConstraint("status <> 'failed' OR error IS NOT NULL", name="failure_explained"),
    )

    id: Mapped[int] = pk()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = utc_now()
    status: Mapped[str] = mapped_column(Text)
    backup_run_id: Mapped[int | None] = mapped_column(BigInteger)
    file_name: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    # Where the file came from: the off-site copy when there is one, else the local directory.
    source: Mapped[str | None] = mapped_column(Text)
    # What the restored database held: its alembic revision, its tables, its audit events and
    # how many of their details digests did not match (must be 0 to succeed).
    schema_revision: Mapped[str | None] = mapped_column(Text)
    table_count: Mapped[int | None] = mapped_column(Integer)
    audit_events: Mapped[int | None] = mapped_column(BigInteger)
    digest_mismatches: Mapped[int | None] = mapped_column(BigInteger)
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
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
        CheckConstraint(
            "mfa_method IS NULL OR mfa_method IN ('provider', 'totp', 'recovery')",
            name="mfa_method_allowed",
        ),
        CheckConstraint("mfa_failures >= 0", name="mfa_failures_nonnegative"),
        CheckConstraint("NOT mfa_pending OR mfa_method IS NULL", name="pending_without_method"),
    )

    id: Mapped[int] = pk()
    token_digest: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # Roles per workspace, {"<workspace id>": ["admin", ...]}; ``roles`` is their union.
    workspace_roles: Mapped[dict[str, list[str]]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = utc_now()
    last_seen_at: Mapped[datetime] = utc_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How a second factor was proven at sign-in (migrations 0044, 0045): "provider" when the ID
    # token's amr proved it, "totp" or "recovery" for built-in TOTP; None when none was.
    mfa_method: Mapped[str | None] = mapped_column(Text)
    # Signed in, with a TOTP code still to enter; such a session grants nothing else.
    mfa_pending: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Wrong codes entered; the session ends at totp.MAX_FAILURES.
    mfa_failures: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))


class UserTotp(Base):
    """A user's authenticator for built-in TOTP (docs/SECURITY.md#built-in-totp).

    The shared secret is sealed like a stored secret, bound to ``totp:<user id>``. Until
    ``confirmed_at`` the user has not yet entered a code from it. ``last_step`` is the last
    accepted time step; only a later one is accepted, so a code works once.
    """

    __tablename__ = "user_totp"
    __table_args__ = (
        CheckConstraint("key_id ~ '^[0-9a-f]{16}$'", name="key_id_format"),
        CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
        CheckConstraint("octet_length(ciphertext) > 16", name="ciphertext_length"),
        CheckConstraint("last_step IS NULL OR last_step >= 0", name="last_step_nonnegative"),
        CheckConstraint(
            "last_step IS NULL OR confirmed_at IS NOT NULL", name="steps_after_confirmation"
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    key_id: Mapped[str] = mapped_column(Text)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    last_step: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = utc_now()
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRecoveryCode(Base):
    """A one-time code for a lost authenticator; only its SHA-256 digest is stored."""

    __tablename__ = "user_recovery_code"
    __table_args__ = (sha256_hex("digest"), UniqueConstraint("user_id", "digest"))

    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    digest: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now()
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
        CheckConstraint(
            "name IN ({}) OR name ~ '^connection_token:[1-9][0-9]*$' "
            "OR name ~ '^connection_webhook_secret:[1-9][0-9]*$'".format(
                ", ".join(f"'{name}'" for name in SECRET_NAMES)
            ),
            name="name_allowed",
        ),
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


TAG_NAME_FORMAT = "^[a-z0-9][a-z0-9._-]{0,62}$"
TAG_TARGET_TYPES = ("group", "project")


class ManagerScope(Base):
    """A GitLab group an administrator assigned to a manager (docs/MANAGER_SPACE.md).

    The manager sees time per person for the group's projects, subgroups included. Rows are
    never rewritten or deleted; a trigger lets an open assignment be ended, once.
    """

    __tablename__ = "manager_scope"
    __table_args__ = (
        Index(
            "uq_manager_scope_open",
            "user_id",
            "group_gitlab_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_manager_scope_user", "user_id"),
        CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)", name="ended_by_whom"),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    group_gitlab_id: Mapped[int] = mapped_column(BigInteger)
    # As the inventory recorded it when assigned.
    group_path: Mapped[str] = mapped_column(Text)
    assigned_by: Mapped[str] = mapped_column(Text)
    assigned_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    assigned_at: Mapped[datetime] = utc_now()
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Tag(Base):
    """A key=value tag kept in Baluarte to group projects (docs/TAGS.md); never sent to GitLab.

    Only the description and colour can change; tags are never deleted.
    """

    __tablename__ = "tag"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", "value"),
        CheckConstraint(f"key ~ '{TAG_NAME_FORMAT}'", name="key_format"),
        CheckConstraint(f"value ~ '{TAG_NAME_FORMAT}'", name="value_format"),
        CheckConstraint("color IS NULL OR color ~ '^#[0-9A-F]{6}$'", name="color_format"),
        CheckConstraint(
            "description IS NULL OR char_length(description) <= 500", name="description_length"
        ),
    )

    id: Mapped[int] = pk()
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    key: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = utc_now()
    updated_at: Mapped[datetime] = utc_now()


class TagAssignment(Base):
    """A tag assigned to a group (and so its subgroups and projects) or to a project.

    Rows are never rewritten or deleted; a trigger lets an open assignment be ended, once.
    """

    __tablename__ = "tag_assignment"
    __table_args__ = (
        Index(
            "uq_tag_assignment_open",
            "tag_id",
            "target_type",
            "target_gitlab_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_tag_assignment_target", "target_type", "target_gitlab_id"),
        one_of("target_type", TAG_TARGET_TYPES),
        CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)", name="ended_by_whom"),
    )

    id: Mapped[int] = pk()
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"))
    target_type: Mapped[str] = mapped_column(Text)
    target_gitlab_id: Mapped[int] = mapped_column(BigInteger)
    target_path: Mapped[str] = mapped_column(Text)
    assigned_by: Mapped[str] = mapped_column(Text)
    assigned_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    assigned_at: Mapped[datetime] = utc_now()
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TimeEntry(Base):
    """One time log from GitLab (docs/TIME_TRACKING.md).

    The summary text is never stored, only its SHA-256. ``removed_at`` marks a log GitLab no
    longer returns when its period is read again (deleted in GitLab); it is kept as history.
    """

    __tablename__ = "time_entry"
    __table_args__ = (
        UniqueConstraint("instance_id", "timelog_id"),
        Index("ix_time_entry_instance_spent", "instance_id", "spent_at"),
        Index("ix_time_entry_project_spent", "project_id", "spent_at"),
        Index("ix_time_entry_user_spent", "username", "spent_at"),
        Index("ix_time_entry_observation", "observation_id"),
        sha256_hex("summary_sha256"),
    )

    id: Mapped[int] = pk()
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"))
    # GitLab's global ID, such as gid://gitlab/Timelog/12.
    timelog_id: Mapped[str] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    # The person's display name in GitLab, as last read.
    user_name: Mapped[str | None] = mapped_column(Text)
    spent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seconds: Mapped[int] = mapped_column(Integer)
    issue_iid: Mapped[int | None] = mapped_column(BigInteger)
    merge_request_iid: Mapped[int | None] = mapped_column(BigInteger)
    # The issue's or merge request's labels and milestone when the log was read.
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    milestone: Mapped[str | None] = mapped_column(Text)
    summary_sha256: Mapped[str | None] = mapped_column(Text)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    created_at: Mapped[datetime] = utc_now()
    updated_at: Mapped[datetime] = utc_now()
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IssueEstimate(Base):
    """An issue's time estimate and total time spent, as GitLab last reported them."""

    __tablename__ = "issue_estimate"
    __table_args__ = (
        Index("ix_issue_estimate_instance", "instance_id"),
        Index("ix_issue_estimate_observation", "observation_id"),
    )

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    issue_iid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    state: Mapped[str | None] = mapped_column(Text)
    estimate_seconds: Mapped[int] = mapped_column(Integer)
    spent_seconds: Mapped[int] = mapped_column(Integer)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    milestone: Mapped[str | None] = mapped_column(Text)
    issue_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    read_at: Mapped[datetime] = utc_now()


class IssueContext(Base):
    """Where an issue with logged time sits in GitLab's planning, as last read.

    Read from the issue's work item: its type and title, parent, top-level ancestor (the first
    of its ancestors, usually an epic), milestone, iteration and estimate. ``iteration_status``
    is ``ok`` or ``unsupported`` (no iterations on this GitLab), so a missing iteration is not
    taken for "none".
    """

    __tablename__ = "issue_context"
    __table_args__ = (
        Index("ix_issue_context_instance", "instance_id"),
        Index("ix_issue_context_observation", "observation_id"),
    )

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    issue_iid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("gitlab_instance.id"))
    work_item_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    web_url: Mapped[str | None] = mapped_column(Text)
    parent_type: Mapped[str | None] = mapped_column(Text)
    parent_reference: Mapped[str | None] = mapped_column(Text)
    parent_title: Mapped[str | None] = mapped_column(Text)
    top_type: Mapped[str | None] = mapped_column(Text)
    top_reference: Mapped[str | None] = mapped_column(Text)
    top_title: Mapped[str | None] = mapped_column(Text)
    milestone: Mapped[str | None] = mapped_column(Text)
    iteration_status: Mapped[str] = mapped_column(Text)
    iteration: Mapped[str | None] = mapped_column(Text)
    estimate_seconds: Mapped[int | None] = mapped_column(Integer)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observation.id"))
    read_at: Mapped[datetime] = utc_now()


class TimeCursor(Base):
    """How far a project's time logs and issue estimates have been read.

    ``history_since`` is where its time history starts: the first collection's look-back.
    """

    __tablename__ = "time_cursor"

    project_id: Mapped[int] = mapped_column(ForeignKey("gitlab_project.id"), primary_key=True)
    history_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_spent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issues_updated_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How far closed issues have been read; null until every closed issue was read once.
    closed_updated_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = utc_now()
