"""Stored evaluation results, evaluation runs and findings followed by policy name (BAL-021).

Evaluations gain the project, the run, the policy name, rule type, severity, every unknown
reason, the value's source and the collection and capability context, and may omit the
observation when the result is not ``pass`` or ``fail`` (a project that was never observed is
still evaluated, as ``unknown``). Findings are keyed by policy name instead of version, record
the latest outcome and how they were resolved. Both tables keep ``policy_name`` equal to the
referenced version's name with the trigger function from 0006.

No released code wrote ``evaluation`` or ``finding`` before this revision, so the new columns
are added as NOT NULL; upgrading fails rather than guessing values if rows exist.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEVERITIES = "'low', 'medium', 'high', 'critical'"
OUTCOMES = "'pass', 'fail', 'unknown', 'unsupported', 'excepted'"


def upgrade() -> None:
    op.create_table(
        "evaluation_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("collection_job_id", sa.BigInteger(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projects", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)",
            name=op.f("ck_evaluation_run_finished_unless_running"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name=op.f("ck_evaluation_run_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["collection_job_id"],
            ["collection_job.id"],
            name=op.f("fk_evaluation_run_collection_job_id_collection_job"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_evaluation_run_instance_id_gitlab_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_run")),
    )
    op.create_index(
        "ix_evaluation_run_started", "evaluation_run", ["instance_id", "started_at"], unique=False
    )

    op.alter_column("evaluation", "observation_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("evaluation", "capability_status", existing_type=sa.Text(), nullable=False)
    for name, column_type, nullable in (
        ("instance_id", sa.BigInteger(), False),
        ("project_id", sa.BigInteger(), False),
        ("evaluation_run_id", sa.BigInteger(), True),
        ("policy_name", sa.Text(), False),
        ("rule_type", sa.Text(), False),
        ("severity", sa.Text(), False),
        ("source", sa.Text(), False),
        ("observed_at", sa.DateTime(timezone=True), True),
        ("capability_name", sa.Text(), True),
        ("collection_kind", sa.Text(), False),
        ("collection_status", sa.Text(), False),
        ("collection_last_success_at", sa.DateTime(timezone=True), True),
        ("ancestry", sa.Text(), False),
    ):
        op.add_column("evaluation", sa.Column(name, column_type, nullable=nullable))
    op.add_column(
        "evaluation",
        sa.Column(
            "unknown_reasons",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    for column, table in (
        ("instance_id", "gitlab_instance"),
        ("project_id", "gitlab_project"),
        ("evaluation_run_id", "evaluation_run"),
    ):
        op.create_foreign_key(
            op.f(f"fk_evaluation_{column}_{table}"), "evaluation", table, [column], ["id"]
        )
    op.create_index(
        "ix_evaluation_project_rule",
        "evaluation",
        ["project_id", "policy_name", "rule_id", "evaluated_at"],
        unique=False,
    )
    op.create_index("ix_evaluation_run", "evaluation", ["evaluation_run_id"], unique=False)
    for name, condition in (
        ("severity_allowed", f"severity IN ({SEVERITIES})"),
        ("ancestry_allowed", "ancestry IN ('complete', 'incomplete')"),
        (
            "capability_status_allowed",
            "capability_status IN ('not_required', 'supported', 'unsupported', 'unknown')",
        ),
        ("unknown_has_reasons", "(outcome = 'unknown') = (cardinality(unknown_reasons) > 0)"),
        ("observed_only_when_decided", "outcome IN ('pass', 'fail') OR observed IS NULL"),
        ("decided_has_evidence", "outcome NOT IN ('pass', 'fail') OR observation_id IS NOT NULL"),
    ):
        op.create_check_constraint(op.f(f"ck_evaluation_{name}"), "evaluation", condition)

    op.drop_constraint("uq_finding_project_id_policy_id_rule_id", "finding", "unique")
    op.add_column("finding", sa.Column("policy_name", sa.Text(), nullable=False))
    op.add_column("finding", sa.Column("latest_outcome", sa.Text(), nullable=False))
    op.add_column("finding", sa.Column("resolution", sa.Text(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_finding_project_id_policy_name_rule_id"),
        "finding",
        ["project_id", "policy_name", "rule_id"],
    )
    op.create_index("ix_finding_state", "finding", ["state", "id"], unique=False)
    for name, condition in (
        ("latest_outcome_allowed", f"latest_outcome IN ({OUTCOMES})"),
        ("resolution_allowed", "resolution IN ('passed', 'no_longer_applies')"),
        ("resolved_has_time", "(state = 'resolved') = (resolved_at IS NOT NULL)"),
        ("resolved_has_resolution", "(state = 'resolved') = (resolution IS NOT NULL)"),
    ):
        op.create_check_constraint(op.f(f"ck_finding_{name}"), "finding", condition)

    for table in ("evaluation", "finding"):
        op.execute(
            f"CREATE TRIGGER {table}_policy_name BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION baluarte_policy_assignment_name()"
        )


def downgrade() -> None:
    for table in ("evaluation", "finding"):
        op.execute(f"DROP TRIGGER {table}_policy_name ON {table}")

    for name in (
        "latest_outcome_allowed",
        "resolution_allowed",
        "resolved_has_time",
        "resolved_has_resolution",
    ):
        op.drop_constraint(op.f(f"ck_finding_{name}"), "finding", "check")
    op.drop_index("ix_finding_state", table_name="finding")
    op.drop_constraint(op.f("uq_finding_project_id_policy_name_rule_id"), "finding", "unique")
    op.drop_column("finding", "resolution")
    op.drop_column("finding", "latest_outcome")
    op.drop_column("finding", "policy_name")
    op.create_unique_constraint(
        "uq_finding_project_id_policy_id_rule_id", "finding", ["project_id", "policy_id", "rule_id"]
    )

    for name in (
        "severity_allowed",
        "ancestry_allowed",
        "capability_status_allowed",
        "unknown_has_reasons",
        "observed_only_when_decided",
        "decided_has_evidence",
    ):
        op.drop_constraint(op.f(f"ck_evaluation_{name}"), "evaluation", "check")
    op.drop_index("ix_evaluation_run", table_name="evaluation")
    op.drop_index("ix_evaluation_project_rule", table_name="evaluation")
    for column, table in (
        ("instance_id", "gitlab_instance"),
        ("project_id", "gitlab_project"),
        ("evaluation_run_id", "evaluation_run"),
    ):
        op.drop_constraint(op.f(f"fk_evaluation_{column}_{table}"), "evaluation", "foreignkey")
    for name in (
        "unknown_reasons",
        "instance_id",
        "project_id",
        "evaluation_run_id",
        "policy_name",
        "rule_type",
        "severity",
        "source",
        "observed_at",
        "capability_name",
        "collection_kind",
        "collection_status",
        "collection_last_success_at",
        "ancestry",
    ):
        op.drop_column("evaluation", name)
    op.alter_column("evaluation", "capability_status", existing_type=sa.Text(), nullable=True)
    op.alter_column("evaluation", "observation_id", existing_type=sa.BigInteger(), nullable=False)

    op.drop_index("ix_evaluation_run_started", table_name="evaluation_run")
    op.drop_table("evaluation_run")
