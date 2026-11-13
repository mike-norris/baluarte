"""Time rules on closed issues: closed issues as read, and approved issue plans (ADR-0011).

New tables:

- ``closed_issue``: each closed issue in monitoring scope as last read: its labels, estimate,
  time spent, merge requests and whether commits are linked to it;
- ``issue_plan``: dry-run plans of labels and estimates to set on closed issues, reviewed and
  then applied, like label plans. A trigger keeps a plan unchanged apart from its lifecycle and
  rejects ``DELETE``.

``time_cursor`` gains ``closed_updated_after``: how far closed issues have been read.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-13 14:00:00.000000
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLAN_HOURS = 24
MAX_STEPS = 5000

ISSUE_PLAN_FUNCTION = """
CREATE FUNCTION baluarte_issue_plan_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'issue plans cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.created_by, NEW.created_by_user_id,
        NEW.created_at, NEW.expires_at, NEW.plan, NEW.plan_digest, NEW.steps, NEW.manual,
        NEW.review_required)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.created_by, OLD.created_by_user_id,
        OLD.created_at, OLD.expires_at, OLD.plan, OLD.plan_digest, OLD.steps, OLD.manual,
        OLD.review_required) THEN
        RAISE EXCEPTION 'an issue plan cannot be rewritten' USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        (OLD.state = 'draft' AND NEW.state IN ('submitted', 'approved', 'expired'))
        OR (OLD.state = 'submitted' AND NEW.state IN ('approved', 'rejected', 'expired'))
        OR (OLD.state = 'approved' AND NEW.state IN ('applying', 'expired'))
        OR (OLD.state = 'applying' AND NEW.state IN ('applied', 'failed'))
    ) THEN
        RAISE EXCEPTION 'an issue plan cannot go from % to %', OLD.state, NEW.state
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state IN ('rejected', 'applied', 'failed', 'expired') THEN
        RAISE EXCEPTION 'a % issue plan cannot change', OLD.state
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _timestamp(name: str, nullable: bool = True) -> sa.Column[datetime]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.add_column(
        "time_cursor",
        sa.Column("closed_updated_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "closed_issue",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_iid", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_type", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column(
            "labels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("estimate_seconds", sa.Integer(), nullable=False),
        sa.Column("spent_seconds", sa.Integer(), nullable=False),
        sa.Column("merge_requests_count", sa.Integer(), nullable=False),
        _timestamp("closed_at"),
        _timestamp("issue_updated_at"),
        sa.Column("commits", sa.Text(), nullable=False),
        _timestamp("commits_for"),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        _timestamp("read_at", nullable=False),
        sa.CheckConstraint(
            "commits IN ('merge_request', 'mention', 'none', 'unknown', 'not_checked')",
            name=op.f("ck_closed_issue_commits_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["gitlab_project.id"],
            name=op.f("fk_closed_issue_project_id_gitlab_project"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_closed_issue_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_closed_issue_observation_id_observation"),
        ),
        sa.PrimaryKeyConstraint("project_id", "issue_iid", name=op.f("pk_closed_issue")),
    )
    op.create_index("ix_closed_issue_instance", "closed_issue", ["instance_id"])
    op.create_index("ix_closed_issue_observation", "closed_issue", ["observation_id"])

    op.create_table(
        "issue_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), server_default="draft", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        _timestamp("created_at", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan_digest", sa.Text(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("manual", sa.Integer(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _timestamp("submitted_at"),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        _timestamp("decided_at"),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("approved_digest", sa.Text(), nullable=True),
        sa.Column("applied_by", sa.Text(), nullable=True),
        _timestamp("apply_started_at"),
        _timestamp("finished_at"),
        sa.Column("failed_step", sa.Integer(), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'submitted', 'approved', 'rejected', 'applying', 'applied', "
            "'failed', 'expired')",
            name=op.f("ck_issue_plan_state_allowed"),
        ),
        sa.CheckConstraint(
            "plan_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_issue_plan_plan_digest_sha256")
        ),
        sa.CheckConstraint(
            f"expires_at > created_at AND expires_at <= created_at + interval '{PLAN_HOURS} hours'",
            name=op.f("ck_issue_plan_expiry_bounded"),
        ),
        sa.CheckConstraint(
            f"steps BETWEEN 0 AND {MAX_STEPS} AND manual >= 0 AND steps + manual >= 1",
            name=op.f("ck_issue_plan_steps_bounded"),
        ),
        sa.CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> created_by_user_id",
            name=op.f("ck_issue_plan_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_issue_plan_decision_complete"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected', 'applying', 'applied', 'failed') "
            "OR decided_at IS NOT NULL",
            name=op.f("ck_issue_plan_decided_has_decision"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'applying', 'applied', 'failed') "
            "OR approved_digest = plan_digest",
            name=op.f("ck_issue_plan_approval_binds_digest"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('draft', 'submitted', 'rejected') OR approved_digest IS NULL",
            name=op.f("ck_issue_plan_unapproved_has_no_digest"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('applying', 'applied', 'failed') OR apply_started_at IS NOT NULL",
            name=op.f("ck_issue_plan_applying_has_start"),
        ),
        sa.CheckConstraint(
            "(state IN ('applied', 'failed', 'expired')) = (finished_at IS NOT NULL)",
            name=op.f("ck_issue_plan_finished_has_time"),
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (failure IS NOT NULL)",
            name=op.f("ck_issue_plan_failure_explained"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_issue_plan_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_issue_plan_created_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_issue_plan_decided_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issue_plan")),
    )
    op.create_index("ix_issue_plan_state", "issue_plan", ["instance_id", "state", "id"])
    op.execute(ISSUE_PLAN_FUNCTION)
    op.execute(
        "CREATE TRIGGER issue_plan_transition BEFORE UPDATE OR DELETE ON issue_plan "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_issue_plan_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER issue_plan_transition ON issue_plan")
    op.execute("DROP FUNCTION baluarte_issue_plan_transition()")
    op.drop_table("issue_plan")
    op.drop_index("ix_closed_issue_observation", table_name="closed_issue")
    op.drop_index("ix_closed_issue_instance", table_name="closed_issue")
    op.drop_table("closed_issue")
    op.drop_column("time_cursor", "closed_updated_after")
