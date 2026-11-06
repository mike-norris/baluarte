"""Label taxonomy, label drift and approved label plans.

Adds ``labels`` as a collection run kind, ``label_taxonomy`` (reviewed versions of the taxonomy)
and ``label_plan`` (dry-run plans and their application). ``app_setting`` allows
``label_write_token`` (what is known about the write token) and ``app_secret`` allows the sealed
``label_write_token`` itself.

Triggers let an update of ``label_taxonomy`` only decide a proposed version, keep a label plan's
document, digest and taxonomy unchanged, and reject ``DELETE`` of either.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-06 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_KINDS = ("capabilities", "groups", "projects", "security_policies", "changes")
SETTING_KEYS = ("oidc", "gitlab", "governance", "operations", "audit_shipping", "change_evidence")
SECRET_NAMES = (
    "oidc_client_secret",
    "gitlab_token",
    "audit_shipping_es_api_key",
    "audit_shipping_es_password",
)
PLAN_HOURS = 24
MAX_STEPS = 1000

TAXONOMY_FUNCTION = """
CREATE FUNCTION baluarte_label_taxonomy_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'label taxonomy versions cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state <> 'proposed' OR NEW.state NOT IN ('approved', 'rejected') THEN
        RAISE EXCEPTION 'only a proposed label taxonomy can be decided'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.version, NEW.name, NEW.source_yaml, NEW.yaml_digest,
        NEW.justification, NEW.review_required, NEW.self_approved, NEW.proposed_by,
        NEW.proposed_by_user_id, NEW.proposed_at)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.version, OLD.name, OLD.source_yaml, OLD.yaml_digest,
        OLD.justification, OLD.review_required, OLD.self_approved, OLD.proposed_by,
        OLD.proposed_by_user_id, OLD.proposed_at) THEN
        RAISE EXCEPTION 'a label taxonomy version cannot be rewritten'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

PLAN_FUNCTION = """
CREATE FUNCTION baluarte_label_plan_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'label plans cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.taxonomy_id, NEW.created_by, NEW.created_by_user_id,
        NEW.created_at, NEW.expires_at, NEW.plan, NEW.plan_digest, NEW.steps, NEW.manual,
        NEW.review_required)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.taxonomy_id, OLD.created_by, OLD.created_by_user_id,
        OLD.created_at, OLD.expires_at, OLD.plan, OLD.plan_digest, OLD.steps, OLD.manual,
        OLD.review_required) THEN
        RAISE EXCEPTION 'a label plan cannot be rewritten' USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        (OLD.state = 'draft' AND NEW.state IN ('submitted', 'approved', 'expired'))
        OR (OLD.state = 'submitted' AND NEW.state IN ('approved', 'rejected', 'expired'))
        OR (OLD.state = 'approved' AND NEW.state IN ('applying', 'expired'))
        OR (OLD.state = 'applying' AND NEW.state IN ('applied', 'failed'))
    ) THEN
        RAISE EXCEPTION 'a label plan cannot go from % to %', OLD.state, NEW.state
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state IN ('rejected', 'applied', 'failed', 'expired') THEN
        RAISE EXCEPTION 'a % label plan cannot change', OLD.state
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _allowed(table: str, column: str, values: Sequence[str], validate: bool = True) -> None:
    name = op.f(f"ck_{table}_{column}_allowed")
    op.drop_constraint(name, table, type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    # NOT VALID on downgrade: rows already recorded stay, and only new rows are checked.
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} "
        f"CHECK ({column} IN ({allowed})){'' if validate else ' NOT VALID'}"
    )


def _timestamp(name: str, nullable: bool = True) -> sa.Column[object]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    _allowed("collection_run", "kind", (*RUN_KINDS, "labels"))
    _allowed("app_setting", "key", (*SETTING_KEYS, "label_write_token"))
    _allowed("app_secret", "name", (*SECRET_NAMES, "label_write_token"))
    op.create_table(
        "label_taxonomy",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_yaml", sa.Text(), nullable=False),
        sa.Column("yaml_digest", sa.Text(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_by_user_id", sa.BigInteger(), nullable=False),
        _timestamp("proposed_at", nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        _timestamp("decided_at"),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('proposed', 'approved', 'rejected')",
            name=op.f("ck_label_taxonomy_state_allowed"),
        ),
        sa.CheckConstraint(
            "yaml_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_label_taxonomy_yaml_digest_sha256")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_label_taxonomy_version_positive")),
        sa.CheckConstraint(
            "justification IS NULL OR length(justification) <= 2000",
            name=op.f("ck_label_taxonomy_justification_length"),
        ),
        sa.CheckConstraint(
            "(state = 'proposed') = (decided_at IS NULL)",
            name=op.f("ck_label_taxonomy_decided_unless_proposed"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL) "
            "AND (decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_label_taxonomy_decision_complete"),
        ),
        sa.CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> proposed_by_user_id",
            name=op.f("ck_label_taxonomy_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "review_required OR state = 'approved'",
            name=op.f("ck_label_taxonomy_unreviewed_is_approved"),
        ),
        sa.CheckConstraint(
            "self_approved = NOT review_required",
            name=op.f("ck_label_taxonomy_self_approved_when_unreviewed"),
        ),
        sa.CheckConstraint(
            "state <> 'rejected' OR length(decision_note) > 0",
            name=op.f("ck_label_taxonomy_rejection_explained"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_label_taxonomy_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_label_taxonomy_proposed_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_label_taxonomy_decided_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_taxonomy")),
        sa.UniqueConstraint("instance_id", "version", name="uq_label_taxonomy_version"),
    )
    op.create_index(
        "uq_label_taxonomy_proposed",
        "label_taxonomy",
        ["instance_id"],
        unique=True,
        postgresql_where=sa.text("state = 'proposed'"),
    )
    op.create_index(
        "ix_label_taxonomy_decided", "label_taxonomy", ["instance_id", "state", "decided_at"]
    )
    op.execute(TAXONOMY_FUNCTION)
    op.execute(
        "CREATE TRIGGER label_taxonomy_transition BEFORE UPDATE OR DELETE ON label_taxonomy "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_label_taxonomy_transition()"
    )
    op.create_table(
        "label_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("taxonomy_id", sa.BigInteger(), nullable=False),
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
            name=op.f("ck_label_plan_state_allowed"),
        ),
        sa.CheckConstraint(
            "plan_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_label_plan_plan_digest_sha256")
        ),
        sa.CheckConstraint(
            f"expires_at > created_at AND expires_at <= created_at + interval '{PLAN_HOURS} hours'",
            name=op.f("ck_label_plan_expiry_bounded"),
        ),
        sa.CheckConstraint(
            f"steps BETWEEN 0 AND {MAX_STEPS} AND manual >= 0 AND steps + manual >= 1",
            name=op.f("ck_label_plan_steps_bounded"),
        ),
        sa.CheckConstraint(
            "NOT review_required OR decided_by_user_id IS NULL "
            "OR decided_by_user_id <> created_by_user_id",
            name=op.f("ck_label_plan_decided_by_another_user"),
        ),
        sa.CheckConstraint(
            "(decided_at IS NULL) = (decided_by_user_id IS NULL)",
            name=op.f("ck_label_plan_decision_complete"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected', 'applying', 'applied', 'failed') "
            "OR decided_at IS NOT NULL",
            name=op.f("ck_label_plan_decided_has_decision"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'applying', 'applied', 'failed') "
            "OR approved_digest = plan_digest",
            name=op.f("ck_label_plan_approval_binds_digest"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('draft', 'submitted', 'rejected') OR approved_digest IS NULL",
            name=op.f("ck_label_plan_unapproved_has_no_digest"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('applying', 'applied', 'failed') OR apply_started_at IS NOT NULL",
            name=op.f("ck_label_plan_applying_has_start"),
        ),
        sa.CheckConstraint(
            "(state IN ('applied', 'failed', 'expired')) = (finished_at IS NOT NULL)",
            name=op.f("ck_label_plan_finished_has_time"),
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (failure IS NOT NULL)",
            name=op.f("ck_label_plan_failure_explained"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["gitlab_instance.id"],
            name=op.f("fk_label_plan_instance_id_gitlab_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["taxonomy_id"],
            ["label_taxonomy.id"],
            name=op.f("fk_label_plan_taxonomy_id_label_taxonomy"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_label_plan_created_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_label_plan_decided_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_plan")),
    )
    op.create_index("ix_label_plan_state", "label_plan", ["instance_id", "state", "id"])
    op.execute(PLAN_FUNCTION)
    op.execute(
        "CREATE TRIGGER label_plan_transition BEFORE UPDATE OR DELETE ON label_plan "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_label_plan_transition()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER label_plan_transition ON label_plan")
    op.execute("DROP FUNCTION baluarte_label_plan_transition()")
    op.drop_table("label_plan")
    op.execute("DROP TRIGGER label_taxonomy_transition ON label_taxonomy")
    op.execute("DROP FUNCTION baluarte_label_taxonomy_transition()")
    op.drop_table("label_taxonomy")
    op.execute("DELETE FROM app_secret WHERE name = 'label_write_token'")
    op.execute("DELETE FROM app_setting WHERE key = 'label_write_token'")
    _allowed("app_secret", "name", SECRET_NAMES)
    _allowed("app_setting", "key", SETTING_KEYS)
    _allowed("collection_run", "kind", RUN_KINDS, validate=False)
