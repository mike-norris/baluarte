"""Change set records (roadmap M14.6, docs/CONFIGURATION_TEMPLATES.md#records).

- ``template_change_set.rationale``: why the change is wanted, 20 to 2,000 characters. A trigger
  keeps it unchanged once set; sets created before this migration have none.
- ``template_change_run``: one row per apply, continue or rollback, with who started it. A
  trigger refuses a run started by one of the set's approvers (its first or final approver)
  unless ``development_execution`` is set (development mode), and refuses any change or
  ``DELETE``: whoever writes to the provider is never someone who approved the set.

Revision ID: 0049
Revises: 0048
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SET = "template_change_set"
RUN = "template_change_run"

RATIONALE_FUNCTION = """
CREATE FUNCTION baluarte_template_change_set_rationale() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.rationale IS NOT NULL AND NEW.rationale IS DISTINCT FROM OLD.rationale THEN
        RAISE EXCEPTION 'a change set''s rationale cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.rationale IS NULL AND NEW.rationale IS NOT NULL THEN
        RAISE EXCEPTION 'a rationale is given when a change set is created'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

RUN_FUNCTION = """
CREATE FUNCTION baluarte_template_change_run_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    found template_change_set%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'template_change_run rows cannot be changed or deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO found FROM template_change_set WHERE id = NEW.change_set_id;
    IF NOT NEW.development_execution
       AND NEW.user_id IN (found.first_approved_by_user_id, found.decided_by_user_id) THEN
        RAISE EXCEPTION 'whoever approved a change set cannot apply, continue or roll it back'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.development_execution
       AND NEW.user_id IS DISTINCT FROM found.first_approved_by_user_id
       AND NEW.user_id IS DISTINCT FROM found.decided_by_user_id THEN
        RAISE EXCEPTION 'development_execution marks a run by an approver'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.add_column(SET, sa.Column("rationale", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f(f"ck_{SET}_rationale_length"),
        SET,
        "rationale IS NULL OR length(rationale) BETWEEN 20 AND 2000",
    )
    op.execute(RATIONALE_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {SET}_rationale BEFORE UPDATE OF rationale ON {SET} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_template_change_set_rationale()"
    )
    op.create_table(
        RUN,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("change_set_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stage_targets", sa.Integer(), nullable=True),
        sa.Column(
            "development_execution",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('apply', 'continue', 'rollback')", name=op.f(f"ck_{RUN}_kind_allowed")
        ),
        sa.CheckConstraint(
            "stage_targets IS NULL OR stage_targets >= 1", name=op.f(f"ck_{RUN}_stage_positive")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name=op.f(f"fk_{RUN}_workspace_id_workspace")
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            [f"{SET}.id"],
            name=op.f(f"fk_{RUN}_change_set_id_{SET}"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f(f"fk_{RUN}_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{RUN}")),
    )
    op.create_index(op.f(f"ix_{RUN}_workspace_id"), RUN, ["workspace_id"])
    op.create_index("ix_template_change_run_change_set", RUN, ["change_set_id", "id"])
    op.execute(RUN_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {RUN}_guard BEFORE INSERT OR UPDATE OR DELETE ON {RUN} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_template_change_run_guard()"
    )


def downgrade() -> None:
    op.drop_table(RUN)
    op.execute("DROP FUNCTION baluarte_template_change_run_guard()")
    op.execute(f"DROP TRIGGER {SET}_rationale ON {SET}")
    op.execute("DROP FUNCTION baluarte_template_change_set_rationale()")
    op.drop_constraint(op.f(f"ck_{SET}_rationale_length"), SET, type_="check")
    op.drop_column(SET, "rationale")
