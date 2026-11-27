"""Evidence items (roadmap M15.2, docs/EVIDENCE_MANAGEMENT.md#evidence-items-m152).

- ``evidence_item``: an uploaded file (in ``captured_file``, by SHA-256) or a reference to a
  document elsewhere, with owner, period, freshness, restriction and review by a second person.
- ``evidence_item_control``: the framework controls an item supports.

Triggers: a draft may change or be deleted; a submitted item may only be accepted or rejected;
a decided item changes only in its reference check and, through retention, in losing its file
(``file_sha256`` to ``removed_sha256``). Control links change only while the item is a draft.

``captured_file`` keeps rejecting ``UPDATE`` and ``DELETE``, except a ``DELETE`` made by
``baluarte_expire_captured_file``: a SECURITY DEFINER function, owned by the schema owner, that
deletes one stored file only when no evidence item and no observation cites it, and returns
whether it did. EXECUTE is revoked from PUBLIC; ``baluarte-migrate`` grants it to the
application's role. Retention calls it after removing expired evidence items' files.

Revision ID: 0051
Revises: 0050
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ITEM = "evidence_item"
LINK = "evidence_item_control"

# What a review never changes: the item as it was submitted.
CONTENT = (
    "id",
    "workspace_id",
    "kind",
    "title",
    "description",
    "owner_user_id",
    "period_start",
    "period_end",
    "valid_until",
    "restricted",
    "supersedes_id",
    "filename",
    "content_type",
    "size_bytes",
    "url",
    "added_by",
    "added_by_user_id",
    "added_at",
    "submitted_at",
)
DECISION = (
    "state",
    "reviewed_by",
    "reviewed_by_user_id",
    "reviewed_at",
    "review_note",
    "development_review",
)
FILE = ("file_sha256", "file_removed_at", "removed_sha256")

# The item's CHECK constraints, as the model declares them.
CHECKS = (
    ("kind_allowed", "kind IN ('file', 'reference')"),
    ("state_allowed", "state IN ('draft', 'submitted', 'accepted', 'rejected')"),
    ("check_status_allowed", "check_status IN ('reachable', 'unreachable')"),
    ("file_sha256_sha256", "file_sha256 ~ '^[0-9a-f]{64}$'"),
    ("check_sha256_sha256", "check_sha256 ~ '^[0-9a-f]{64}$'"),
    ("removed_sha256_sha256", "removed_sha256 ~ '^[0-9a-f]{64}$'"),
    ("title_length", "length(title) BETWEEN 3 AND 200"),
    ("description_length", "length(description) <= 4000"),
    ("url_form", "url IS NULL OR (length(url) <= 2000 AND url ~ '^https?://')"),
    ("reference_has_url", "(kind = 'reference') = (url IS NOT NULL)"),
    (
        "file_only_for_files",
        "kind = 'file' OR (file_sha256 IS NULL AND file_removed_at IS NULL)",
    ),
    (
        "submitted_with_file",
        "state = 'draft' OR kind = 'reference' OR file_sha256 IS NOT NULL "
        "OR file_removed_at IS NOT NULL",
    ),
    ("size_bounds", "size_bytes IS NULL OR size_bytes BETWEEN 1 AND 26214400"),
    ("period_order", "period_start IS NULL OR period_end IS NULL OR period_end >= period_start"),
    (
        "reviewed_when_decided",
        "(state IN ('accepted', 'rejected')) = (reviewed_at IS NOT NULL) "
        "AND (reviewed_at IS NULL) = (reviewed_by_user_id IS NULL)",
    ),
    ("submitted_unless_draft", "(state = 'draft') = (submitted_at IS NULL)"),
    (
        "reviewed_by_another_user",
        "reviewed_by_user_id IS NULL OR reviewed_by_user_id <> added_by_user_id "
        "OR development_review",
    ),
    (
        "development_review_by_adder",
        "NOT development_review OR reviewed_by_user_id = added_by_user_id",
    ),
    ("rejection_explained", "state <> 'rejected' OR length(review_note) > 0"),
)


def _row(prefix: str, columns: Sequence[str]) -> str:
    return ", ".join(f"{prefix}.{column}" for column in columns)


ITEM_TRANSITION = f"""
CREATE FUNCTION baluarte_evidence_item_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.state = 'draft' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'only a draft evidence item can be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.workspace_id, NEW.kind, NEW.added_by, NEW.added_by_user_id, NEW.added_at)
       IS DISTINCT FROM
       (OLD.id, OLD.workspace_id, OLD.kind, OLD.added_by, OLD.added_by_user_id, OLD.added_at) THEN
        RAISE EXCEPTION 'who added an evidence item, and its kind, cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'draft' THEN
        IF NEW.state IN ('draft', 'submitted') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'a draft evidence item is submitted before it is reviewed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({_row("NEW", CONTENT)}) IS DISTINCT FROM ({_row("OLD", CONTENT)}) THEN
        RAISE EXCEPTION 'a submitted evidence item cannot change; add a new one'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'submitted' THEN
        IF NEW.state IN ('accepted', 'rejected', 'submitted')
           AND ({_row("NEW", FILE)}) IS NOT DISTINCT FROM ({_row("OLD", FILE)}) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'a submitted evidence item can only be accepted or rejected'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({_row("NEW", DECISION)}) IS DISTINCT FROM ({_row("OLD", DECISION)}) THEN
        RAISE EXCEPTION 'a reviewed evidence item''s decision cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({_row("NEW", FILE)}) IS DISTINCT FROM ({_row("OLD", FILE)})
       AND NOT (OLD.file_sha256 IS NOT NULL AND NEW.file_sha256 IS NULL
                AND NEW.removed_sha256 = OLD.file_sha256 AND NEW.file_removed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'a reviewed evidence item''s file can only be removed by retention'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

LINK_DRAFT_ONLY = """
CREATE FUNCTION baluarte_evidence_item_control_draft_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    item_state text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'an evidence item''s control links are added or removed, never changed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    SELECT state INTO item_state FROM evidence_item
     WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.evidence_item_id ELSE NEW.evidence_item_id END;
    -- A deleted draft takes its links with it (ON DELETE CASCADE): no row is left to check.
    IF item_state IS NULL OR item_state = 'draft' THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    RAISE EXCEPTION 'controls are linked only while an evidence item is a draft'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

CAPTURED_FILE_IMMUTABLE = """
CREATE OR REPLACE FUNCTION baluarte_captured_file_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('baluarte.file_expiry', true) = 'on'
       AND current_user = (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID)
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'captured files cannot be changed or deleted'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

CAPTURED_FILE_ORIGINAL = """
CREATE OR REPLACE FUNCTION baluarte_captured_file_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'captured files cannot be changed or deleted'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

EXPIRE_FILE = """
CREATE FUNCTION baluarte_expire_captured_file(file_sha256 text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    gone bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM evidence_item WHERE evidence_item.file_sha256 = $1)
       OR EXISTS (
           SELECT 1 FROM observation
            WHERE payload @> jsonb_build_object(
                'attachments', jsonb_build_array(jsonb_build_object('sha256', $1))
            )
       ) THEN
        RETURN false;
    END IF;
    PERFORM set_config('baluarte.file_expiry', 'on', true);
    DELETE FROM captured_file WHERE sha256 = $1;
    GET DIAGNOSTICS gone = ROW_COUNT;
    PERFORM set_config('baluarte.file_expiry', 'off', true);
    RETURN gone > 0;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        ITEM,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("restricted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("state", sa.Text(), server_default="draft", nullable=False),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.Column("file_sha256", sa.Text(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("file_removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_sha256", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("check_status", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_http_status", sa.Integer(), nullable=True),
        sa.Column("check_sha256", sa.Text(), nullable=True),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("added_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "development_review", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name=op.f(f"fk_{ITEM}_workspace_id_workspace")
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], [f"{ITEM}.id"], name=op.f(f"fk_{ITEM}_supersedes_id_{ITEM}")
        ),
        sa.ForeignKeyConstraint(
            ["file_sha256"],
            ["captured_file.sha256"],
            name=op.f(f"fk_{ITEM}_file_sha256_captured_file"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{ITEM}")),
        *(sa.CheckConstraint(rule, name=op.f(f"ck_{ITEM}_{name}")) for name, rule in CHECKS),
    )
    op.create_index("ix_evidence_item_workspace", ITEM, ["workspace_id", "state", "id"])
    op.create_table(
        LINK,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("evidence_item_id", sa.BigInteger(), nullable=False),
        sa.Column("framework_control_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_item_id"],
            [f"{ITEM}.id"],
            name=op.f(f"fk_{LINK}_evidence_item_id_{ITEM}"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["framework_control_id"],
            ["framework_control.id"],
            name=op.f(f"fk_{LINK}_framework_control_id_framework_control"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{LINK}")),
        sa.UniqueConstraint(
            "evidence_item_id",
            "framework_control_id",
            name=op.f(f"uq_{LINK}_evidence_item_id_framework_control_id"),
        ),
    )
    op.create_index("ix_evidence_item_control_control", LINK, ["framework_control_id"])
    op.execute(ITEM_TRANSITION)
    op.execute(
        f"CREATE TRIGGER {ITEM}_transition BEFORE UPDATE OR DELETE ON {ITEM} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_evidence_item_transition()"
    )
    op.execute(LINK_DRAFT_ONLY)
    op.execute(
        f"CREATE TRIGGER {LINK}_draft_only BEFORE INSERT OR UPDATE OR DELETE ON {LINK} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_evidence_item_control_draft_only()"
    )
    op.execute(CAPTURED_FILE_IMMUTABLE)
    op.execute(EXPIRE_FILE)
    op.execute("REVOKE ALL ON FUNCTION baluarte_expire_captured_file(text) FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION baluarte_expire_captured_file(text)")
    op.execute(CAPTURED_FILE_ORIGINAL)
    op.drop_table(LINK)
    op.execute("DROP FUNCTION baluarte_evidence_item_control_draft_only()")
    op.drop_table(ITEM)
    op.execute("DROP FUNCTION baluarte_evidence_item_transition()")
