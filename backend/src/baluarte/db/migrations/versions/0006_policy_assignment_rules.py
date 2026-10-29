"""Policy assignments pin one version per policy name and target.

Assignments gain the instance and the policy name, so a target can hold at most one version of
each policy (BAL-018). The unused ``precedence`` column is dropped: precedence follows from the
GitLab hierarchy. A trigger keeps ``policy_name`` equal to the name of the referenced version.

No released code wrote ``policy_assignment`` before this revision, so the new columns are added
as NOT NULL; upgrading fails rather than guessing values if rows exist.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_policy_assignment_policy_id_target_type_target_id", "policy_assignment", "unique"
    )
    op.drop_column("policy_assignment", "precedence")
    op.add_column("policy_assignment", sa.Column("instance_id", sa.BigInteger(), nullable=False))
    op.add_column("policy_assignment", sa.Column("policy_name", sa.Text(), nullable=False))
    op.create_foreign_key(
        op.f("fk_policy_assignment_instance_id_gitlab_instance"),
        "policy_assignment",
        "gitlab_instance",
        ["instance_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_policy_assignment_target_policy_name",
        "policy_assignment",
        ["instance_id", "target_type", "target_id", "policy_name"],
    )
    op.create_index("ix_policy_assignment_policy_id", "policy_assignment", ["policy_id"])
    op.create_check_constraint(
        op.f("ck_policy_assignment_instance_target"),
        "policy_assignment",
        "target_type <> 'instance' OR target_id = instance_id",
    )
    op.execute(
        """
        CREATE FUNCTION baluarte_policy_assignment_name() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.policy_name IS DISTINCT FROM (SELECT name FROM policy WHERE id = NEW.policy_id)
            THEN
                RAISE EXCEPTION 'policy_name does not match the name of policy %', NEW.policy_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER policy_assignment_name BEFORE INSERT OR UPDATE ON policy_assignment "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_policy_assignment_name()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER policy_assignment_name ON policy_assignment")
    op.execute("DROP FUNCTION baluarte_policy_assignment_name()")
    op.drop_constraint(op.f("ck_policy_assignment_instance_target"), "policy_assignment", "check")
    op.drop_index("ix_policy_assignment_policy_id", table_name="policy_assignment")
    op.drop_constraint("uq_policy_assignment_target_policy_name", "policy_assignment", "unique")
    op.drop_constraint(
        op.f("fk_policy_assignment_instance_id_gitlab_instance"), "policy_assignment", "foreignkey"
    )
    op.drop_column("policy_assignment", "policy_name")
    op.drop_column("policy_assignment", "instance_id")
    op.add_column(
        "policy_assignment",
        sa.Column("precedence", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_unique_constraint(
        "uq_policy_assignment_policy_id_target_type_target_id",
        "policy_assignment",
        ["policy_id", "target_type", "target_id"],
    )
