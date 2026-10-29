"""Observations reference the collection run that read them (BAL-026).

Adds ``observation.collection_run_id`` and an index for the latest instance-level observation
of each source (capabilities). Existing observations, if any, keep a null run.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("observation", sa.Column("collection_run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_observation_collection_run_id_collection_run"),
        "observation",
        "collection_run",
        ["collection_run_id"],
        ["id"],
    )
    op.create_index(
        "ix_observation_instance_collected",
        "observation",
        ["instance_id", "source", "collected_at"],
        unique=False,
        postgresql_where=sa.text("project_id IS NULL AND group_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_observation_instance_collected",
        table_name="observation",
        postgresql_where=sa.text("project_id IS NULL AND group_id IS NULL"),
    )
    op.drop_constraint(
        op.f("fk_observation_collection_run_id_collection_run"), "observation", type_="foreignkey"
    )
    op.drop_column("observation", "collection_run_id")
