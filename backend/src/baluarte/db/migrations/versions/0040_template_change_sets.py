"""Template change sets and saved selections (roadmap M14.3, docs/CONFIGURATION_TEMPLATES.md).

A change set is an immutable document of exact targets with before and after values, built from
stored evidence; its digest binds approval. A trigger allows only

- ``proposed`` -> ``proposed`` recording the first of two approvals of a sensitive set;
- ``proposed`` -> ``approved`` or ``rejected``;
- ``proposed`` -> ``withdrawn`` by its creator;
- ``proposed`` or ``approved`` -> ``expired`` once ``expires_at`` has passed;

keeps the document and every recorded decision unchanged and rejects ``DELETE``. CHECK
constraints require approvers other than the creator, and for a sensitive set two different
approvers, unless the creator approved in development mode (``development_decision``).

A saved selection names target criteria to reuse; it is deleted when no longer wanted.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-24 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_CHANGE_SET_HOURS = 7 * 24

TRANSITION_FUNCTION = """
CREATE FUNCTION baluarte_template_change_set_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'template_change_set rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.workspace_id, NEW.instance_id, NEW.title, NEW.document, NEW.digest,
        NEW.targets, NEW.changes, NEW.skipped, NEW.sensitive, NEW.created_by_user_id,
        NEW.created_at, NEW.expires_at)
       IS DISTINCT FROM
       (OLD.id, OLD.workspace_id, OLD.instance_id, OLD.title, OLD.document, OLD.digest,
        OLD.targets, OLD.changes, OLD.skipped, OLD.sensitive, OLD.created_by_user_id,
        OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'a template change set cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.first_approved_at IS NOT NULL AND
       (NEW.first_approved_by_user_id, NEW.first_approved_at, NEW.first_approval_note)
       IS DISTINCT FROM
       (OLD.first_approved_by_user_id, OLD.first_approved_at, OLD.first_approval_note) THEN
        RAISE EXCEPTION 'a recorded approval cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.decided_at IS NOT NULL AND
       (NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note, NEW.approved_digest,
        NEW.development_decision)
       IS DISTINCT FROM
       (OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note, OLD.approved_digest,
        OLD.development_decision) THEN
        RAISE EXCEPTION 'a template change set decision cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'proposed' AND NEW.state IN ('proposed', 'approved', 'rejected') THEN
        IF NEW.state <> 'rejected' AND OLD.expires_at <= now() THEN
            RAISE EXCEPTION 'an expired template change set cannot be approved'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'proposed' AND NEW.state = 'withdrawn' THEN
        RETURN NEW;
    END IF;
    IF OLD.state IN ('proposed', 'approved') AND NEW.state = 'expired' THEN
        IF OLD.expires_at > now() THEN
            RAISE EXCEPTION 'a template change set cannot expire before expires_at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'template change set cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def upgrade() -> None:
    table = "template_change_set"
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("targets", sa.Integer(), nullable=False),
        sa.Column("changes", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_approved_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("first_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_approval_note", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("approved_digest", sa.Text(), nullable=True),
        sa.Column(
            "development_decision", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('proposed', 'approved', 'rejected', 'withdrawn', 'expired')",
            name=op.f(f"ck_{table}_state_allowed"),
        ),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name=op.f(f"ck_{table}_digest_sha256")),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 200", name=op.f(f"ck_{table}_title_length")
        ),
        sa.CheckConstraint(
            "targets >= 1 AND changes >= 1 AND skipped >= 0", name=op.f(f"ck_{table}_counts")
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + "
            f"interval '{MAX_CHANGE_SET_HOURS} hours'",
            name=op.f(f"ck_{table}_expiry_bounded"),
        ),
        sa.CheckConstraint(
            "(first_approved_at IS NULL) = (first_approved_by_user_id IS NULL)",
            name=op.f(f"ck_{table}_first_approval_complete"),
        ),
        sa.CheckConstraint(
            "sensitive OR first_approved_at IS NULL",
            name=op.f(f"ck_{table}_first_approval_when_sensitive"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f(f"ck_{table}_decision_complete"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name=op.f(f"ck_{table}_decided_has_decision"),
        ),
        sa.CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0",
            name=op.f(f"ck_{table}_rejection_explained"),
        ),
        sa.CheckConstraint(
            "state <> 'approved' OR approved_digest IS NOT NULL",
            name=op.f(f"ck_{table}_approved_has_digest"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('proposed', 'rejected', 'withdrawn') OR approved_digest IS NULL",
            name=op.f(f"ck_{table}_unapproved_has_no_digest"),
        ),
        sa.CheckConstraint(
            "approved_digest IS NULL OR approved_digest = digest",
            name=op.f(f"ck_{table}_approval_binds_digest"),
        ),
        sa.CheckConstraint(
            "NOT sensitive OR state <> 'approved' OR first_approved_at IS NOT NULL",
            name=op.f(f"ck_{table}_sensitive_approved_twice"),
        ),
        sa.CheckConstraint(
            "development_decision OR ("
            "(decided_by_user_id IS NULL OR decided_by_user_id <> created_by_user_id) "
            "AND (first_approved_by_user_id IS NULL "
            "OR (first_approved_by_user_id <> created_by_user_id "
            "AND first_approved_by_user_id IS DISTINCT FROM decided_by_user_id)))",
            name=op.f(f"ck_{table}_decided_by_others"),
        ),
        sa.CheckConstraint(
            "NOT development_decision OR decided_by_user_id = created_by_user_id "
            "OR first_approved_by_user_id = created_by_user_id",
            name=op.f(f"ck_{table}_development_decision_by_proposer"),
        ),
        sa.CheckConstraint(
            "(state IN ('withdrawn', 'expired')) = (ended_at IS NOT NULL)",
            name=op.f(f"ck_{table}_ended_has_time"),
        ),
        _fk(table, "workspace_id", "workspace"),
        _fk(table, "instance_id", "gitlab_instance"),
        _fk(table, "created_by_user_id", "app_user"),
        _fk(table, "first_approved_by_user_id", "app_user"),
        _fk(table, "decided_by_user_id", "app_user"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(op.f(f"ix_{table}_workspace_id"), table, ["workspace_id"])
    op.create_index(f"ix_{table}_state", table, ["instance_id", "state", "id"])
    op.execute(TRANSITION_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {table}_transition BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_template_change_set_transition()"
    )

    saved = "saved_selection"
    op.create_table(
        saved,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("selection", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name=op.f(f"ck_{saved}_name_length")),
        _fk(saved, "workspace_id", "workspace"),
        _fk(saved, "instance_id", "gitlab_instance"),
        _fk(saved, "created_by_user_id", "app_user"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{saved}")),
        sa.UniqueConstraint("instance_id", "name", name=op.f(f"uq_{saved}_instance_id_name")),
    )
    op.create_index(op.f(f"ix_{saved}_workspace_id"), saved, ["workspace_id"])


def downgrade() -> None:
    op.drop_table("saved_selection")
    op.execute("DROP TRIGGER template_change_set_transition ON template_change_set")
    op.drop_table("template_change_set")
    op.execute("DROP FUNCTION baluarte_template_change_set_transition()")
