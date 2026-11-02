"""Archived projects are not evaluated.

Adds ``project_archived`` as a finding resolution: an archived project is no longer checked, and
its open or excepted findings are resolved with this resolution.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-01 20:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _resolutions(*resolutions: str, validate: bool = True) -> None:
    op.drop_constraint(op.f("ck_finding_resolution_allowed"), "finding", type_="check")
    allowed = ", ".join(f"'{resolution}'" for resolution in resolutions)
    # NOT VALID on downgrade: findings already resolved as project_archived stay, and only new
    # or changed rows are checked.
    op.execute(
        "ALTER TABLE finding ADD CONSTRAINT ck_finding_resolution_allowed "
        f"CHECK (resolution IN ({allowed})){'' if validate else ' NOT VALID'}"
    )


def upgrade() -> None:
    _resolutions("passed", "no_longer_applies", "project_archived")


def downgrade() -> None:
    _resolutions("passed", "no_longer_applies", validate=False)
