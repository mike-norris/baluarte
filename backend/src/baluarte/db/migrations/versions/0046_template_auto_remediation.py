"""Automatic drift remediation, proposing only (roadmap M14.5,
docs/CONFIGURATION_TEMPLATES.md#automatic-proposals).

- ``template_assignment.auto_remediate``: when on, Baluarte proposes a change set fixing the
  drift of the settings this assignment decides. Off by default. It never approves or applies.
- ``template_change_set.created_by_system``: a set Baluarte proposed itself has no creating
  user; anyone with ``changes:approve`` decides it, and a sensitive one still needs two people.

Revision ID: 0046
Revises: 0045
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSIGNMENT = "template_assignment"
CHANGE_SET = "template_change_set"


def upgrade() -> None:
    op.add_column(
        ASSIGNMENT,
        sa.Column("auto_remediate", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        CHANGE_SET,
        sa.Column("created_by_system", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.alter_column(CHANGE_SET, "created_by_user_id", nullable=True)
    op.create_check_constraint(
        op.f(f"ck_{CHANGE_SET}_system_or_user_creator"),
        CHANGE_SET,
        "(created_by_user_id IS NULL) = created_by_system",
    )
    op.create_check_constraint(
        op.f(f"ck_{CHANGE_SET}_system_sets_not_development"),
        CHANGE_SET,
        "NOT (created_by_system AND development_decision)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f(f"ck_{CHANGE_SET}_system_sets_not_development"), CHANGE_SET, type_="check"
    )
    op.drop_constraint(op.f(f"ck_{CHANGE_SET}_system_or_user_creator"), CHANGE_SET, type_="check")
    # Downgrading with system-proposed sets present fails here: they have no creating user.
    op.alter_column(CHANGE_SET, "created_by_user_id", nullable=False)
    op.drop_column(CHANGE_SET, "created_by_system")
    op.drop_column(ASSIGNMENT, "auto_remediate")
