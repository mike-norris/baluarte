"""Initial schema.

Creates the tables in docs/DATA_MODEL.md and triggers that make evidence rows immutable:
observation and evaluation reject UPDATE; audit_event rejects UPDATE and DELETE.
Observation and evaluation deletes stay possible for the retention design (BAL-029).

Revision ID: 0001
Revises:
Create Date: 2026-10-28 10:46:34.749936
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE = {
    "observation": "UPDATE",
    "evaluation": "UPDATE",
    "audit_event": "UPDATE OR DELETE",
}


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("details_digest", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "details_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_audit_event_details_digest_sha256")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index("ix_audit_event_occurred", "audit_event", ["occurred_at"], unique=False)
    op.create_table(
        "collection_job",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_collection_job_state_allowed"),
        ),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_collection_job_attempt_non_negative")),
        sa.CheckConstraint(
            "max_attempts >= 1", name=op.f("ck_collection_job_max_attempts_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_job")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_collection_job_idempotency_key")),
    )
    op.create_index(
        "ix_collection_job_claimable", "collection_job", ["state", "run_after"], unique=False
    )
    op.create_table(
        "export_job",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("manifest_digest", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_export_job_manifest_digest_sha256")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_export_job_status_allowed"),
        ),
        sa.CheckConstraint("period_end > period_start", name=op.f("ck_export_job_period_ordered")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_job")),
    )
    op.create_table(
        "gitlab_instance",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("edition", sa.Text(), nullable=True),
        sa.Column("capability_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gitlab_instance")),
        sa.UniqueConstraint("base_url", name=op.f("uq_gitlab_instance_base_url")),
    )
    op.create_table(
        "policy",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_yaml", sa.Text(), nullable=False),
        sa.Column("yaml_digest", sa.Text(), nullable=False),
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "yaml_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_policy_yaml_digest_sha256")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_policy_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy")),
        sa.UniqueConstraint("name", "version", name=op.f("uq_policy_name_version")),
    )
    op.create_table(
        "gitlab_group",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("gitlab_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_gitlab_id", sa.BigInteger(), nullable=True),
        sa.Column("full_path", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('active', 'missing', 'inaccessible')",
            name=op.f("ck_gitlab_group_state_allowed"),
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'internal', 'public')",
            name=op.f("ck_gitlab_group_visibility_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_gitlab_group_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gitlab_group")),
        sa.UniqueConstraint(
            "instance_id", "gitlab_id", name=op.f("uq_gitlab_group_instance_id_gitlab_id")
        ),
    )
    op.create_index(
        "ix_gitlab_group_parent", "gitlab_group", ["instance_id", "parent_gitlab_id"], unique=False
    )
    op.create_table(
        "gitlab_project",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("gitlab_id", sa.BigInteger(), nullable=False),
        sa.Column("namespace_gitlab_id", sa.BigInteger(), nullable=False),
        sa.Column("path_with_namespace", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('active', 'missing', 'inaccessible')",
            name=op.f("ck_gitlab_project_state_allowed"),
        ),
        sa.CheckConstraint(
            "visibility IN ('private', 'internal', 'public')",
            name=op.f("ck_gitlab_project_visibility_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_gitlab_project_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gitlab_project")),
        sa.UniqueConstraint(
            "instance_id", "gitlab_id", name=op.f("uq_gitlab_project_instance_id_gitlab_id")
        ),
    )
    op.create_index(
        "ix_gitlab_project_namespace",
        "gitlab_project",
        ["instance_id", "namespace_gitlab_id"],
        unique=False,
    )
    op.create_table(
        "policy_assignment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("policy_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("precedence", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_type IN ('instance', 'group', 'project')",
            name=op.f("ck_policy_assignment_target_type_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["policy.id"], name=op.f("fk_policy_assignment_policy_id_policy")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_assignment")),
        sa.UniqueConstraint(
            "policy_id",
            "target_type",
            "target_id",
            name=op.f("uq_policy_assignment_policy_id_target_type_target_id"),
        ),
    )
    op.create_table(
        "observation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("collection_job_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collection_status", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload_digest", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "collection_status IN ('ok', 'partial', 'forbidden', 'not_found', 'unsupported', 'failed')",
            name=op.f("ck_observation_collection_status_allowed"),
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_observation_payload_digest_sha256")
        ),
        sa.CheckConstraint(
            "(payload IS NULL) = (payload_digest IS NULL)",
            name=op.f("ck_observation_payload_has_digest"),
        ),
        sa.CheckConstraint(
            "project_id IS NULL OR group_id IS NULL", name=op.f("ck_observation_single_target")
        ),
        sa.ForeignKeyConstraint(
            ["collection_job_id"],
            ["collection_job.id"],
            name=op.f("fk_observation_collection_job_id_collection_job"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["gitlab_group.id"], name=op.f("fk_observation_group_id_gitlab_group")
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_observation_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_observation_project_id_gitlab_project"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observation")),
    )
    op.create_index(
        "ix_observation_group_collected", "observation", ["group_id", "collected_at"], unique=False
    )
    op.create_index(
        "ix_observation_project_collected",
        "observation",
        ["project_id", "collected_at"],
        unique=False,
    )
    op.create_table(
        "evaluation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("capability_status", sa.Text(), nullable=True),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('pass', 'fail', 'unknown', 'unsupported', 'excepted')",
            name=op.f("ck_evaluation_outcome_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_evaluation_observation_id_observation"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["policy.id"], name=op.f("fk_evaluation_policy_id_policy")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation")),
    )
    op.create_index(
        op.f("ix_evaluation_observation_id"), "evaluation", ["observation_id"], unique=False
    )
    op.create_index(
        "ix_evaluation_policy_rule", "evaluation", ["policy_id", "rule_id"], unique=False
    )
    op.create_table(
        "finding",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("latest_evaluation_id", sa.BigInteger(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="open", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_finding_severity_allowed"),
        ),
        sa.CheckConstraint(
            "state IN ('open', 'resolved', 'excepted')", name=op.f("ck_finding_state_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["latest_evaluation_id"],
            ["evaluation.id"],
            name=op.f("fk_finding_latest_evaluation_id_evaluation"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["policy.id"], name=op.f("fk_finding_policy_id_policy")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["gitlab_project.id"], name=op.f("fk_finding_project_id_gitlab_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finding")),
        sa.UniqueConstraint(
            "project_id",
            "policy_id",
            "rule_id",
            name=op.f("uq_finding_project_id_policy_id_rule_id"),
        ),
    )
    op.execute(
        """
        CREATE FUNCTION baluarte_reject_modification() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% on % is not allowed: rows are immutable evidence', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    for table, events in IMMUTABLE.items():
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE {events} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION baluarte_reject_modification()"
        )


def downgrade() -> None:
    for table in IMMUTABLE:
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.execute("DROP FUNCTION baluarte_reject_modification()")
    op.drop_table("finding")
    op.drop_index("ix_evaluation_policy_rule", table_name="evaluation")
    op.drop_index(op.f("ix_evaluation_observation_id"), table_name="evaluation")
    op.drop_table("evaluation")
    op.drop_index("ix_observation_project_collected", table_name="observation")
    op.drop_index("ix_observation_group_collected", table_name="observation")
    op.drop_table("observation")
    op.drop_table("policy_assignment")
    op.drop_index("ix_gitlab_project_namespace", table_name="gitlab_project")
    op.drop_table("gitlab_project")
    op.drop_index("ix_gitlab_group_parent", table_name="gitlab_group")
    op.drop_table("gitlab_group")
    op.drop_table("policy")
    op.drop_table("gitlab_instance")
    op.drop_table("export_job")
    op.drop_index("ix_collection_job_claimable", table_name="collection_job")
    op.drop_table("collection_job")
    op.drop_index("ix_audit_event_occurred", table_name="audit_event")
    op.drop_table("audit_event")
