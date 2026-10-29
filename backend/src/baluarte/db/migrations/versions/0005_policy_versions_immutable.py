"""Policy versions are immutable.

A saved baseline version is evidence of what was in force: changing it means saving a new
version (BAL-017). Reuses the trigger function from 0001.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TRIGGER policy_immutable BEFORE UPDATE ON policy "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_reject_modification()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER policy_immutable ON policy")
