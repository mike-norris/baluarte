"""Roles per workspace, and audit events that name their workspace (roadmap M11.4).

- ``workspace.group_roles``: identity provider groups per role for that workspace, as
  ``{"admin": ["group", ...], ...}``. NULL for the default workspace, which uses the sign-in
  settings' mapping as before.
- ``user_session.workspace_roles``: the roles a session holds in each workspace, resolved at
  sign-in, as ``{"<workspace id>": ["admin", ...]}``. Existing sessions hold their roles in the
  default workspace. ``user_session.roles`` stays the union of them.
- ``audit_event.workspace_id``: the workspace a request or job acted in. Existing events keep
  NULL and are not rewritten; the digest covers ``details`` only, so every digest still verifies.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-21 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workspace", sa.Column("group_roles", postgresql.JSONB(astext_type=sa.Text())))
    op.create_check_constraint(
        op.f("ck_workspace_group_roles_object"),
        "workspace",
        "group_roles IS NULL OR jsonb_typeof(group_roles) = 'object'",
    )
    op.add_column(
        "user_session",
        sa.Column(
            "workspace_roles",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE user_session SET workspace_roles = jsonb_build_object("
        "(SELECT id FROM workspace WHERE slug = 'default')::text, to_jsonb(roles))"
    )
    op.add_column("audit_event", sa.Column("workspace_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audit_event_workspace_id_workspace"),
        "audit_event",
        "workspace",
        ["workspace_id"],
        ["id"],
    )
    op.create_index(op.f("ix_audit_event_workspace_id"), "audit_event", ["workspace_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_event_workspace_id"), table_name="audit_event")
    op.drop_constraint(
        op.f("fk_audit_event_workspace_id_workspace"), "audit_event", type_="foreignkey"
    )
    op.drop_column("audit_event", "workspace_id")
    op.drop_column("user_session", "workspace_roles")
    op.drop_constraint(op.f("ck_workspace_group_roles_object"), "workspace", type_="check")
    op.drop_column("workspace", "group_roles")
