"""Applying template change sets (roadmap M14.4, docs/CONFIGURATION_TEMPLATES.md#execution).

``template_change_set`` gains the execution states and who started, continued, cancelled or
rolled back a set. Its trigger now also allows

- ``approved`` -> ``applying`` before ``expires_at``;
- ``applying`` -> ``paused`` (a stage is done and targets remain), ``finished`` or
  ``cancelled``; ``paused`` -> ``applying`` or ``cancelled``;
- ``paused``, ``cancelled`` or ``finished`` -> ``rolling_back`` -> ``rolled_back``;

and still keeps the document and decisions unchanged and rejects ``DELETE``.

``template_change_item`` holds one change of the set: what was approved, never changed, and its
progress (status, attempts, the value read just before writing, what a read showed afterwards,
and the rollback). A trigger keeps the approved columns unchanged and rejects ``DELETE``.

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-24 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "template_change_set"
TOKEN_NAME = "template_write_token"  # noqa: S105 (a secret's name, not a secret)
SETTING_KEYS = (
    "oidc",
    "gitlab",
    "governance",
    "operations",
    "audit_shipping",
    "change_evidence",
    "label_write_token",
    "time_tracking",
    "notifications",
    "remediation_write_token",
)
SECRET_NAMES = (
    "oidc_client_secret",
    "gitlab_token",
    "audit_shipping_es_api_key",
    "audit_shipping_es_password",
    "label_write_token",
    "notifications_smtp_password",
    "notifications_webhook_url",
    "remediation_write_token",
)
PER_CONNECTION = (
    "name ~ '^connection_token:[1-9][0-9]*$' OR name ~ '^connection_webhook_secret:[1-9][0-9]*$'"
)


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _names(settings: Sequence[str], secrets: Sequence[str]) -> None:
    """Replace the allowed ``app_setting`` keys and ``app_secret`` names."""
    op.drop_constraint(op.f("ck_app_setting_key_allowed"), "app_setting", type_="check")
    op.create_check_constraint(
        op.f("ck_app_setting_key_allowed"), "app_setting", f"key IN ({_in(settings)})"
    )
    op.drop_constraint(op.f("ck_app_secret_name_allowed"), "app_secret", type_="check")
    op.create_check_constraint(
        op.f("ck_app_secret_name_allowed"),
        "app_secret",
        f"name IN ({_in(secrets)}) OR {PER_CONNECTION}",
    )


OLD_STATES = "'proposed', 'approved', 'rejected', 'withdrawn', 'expired'"
NEW_STATES = (
    "'proposed', 'approved', 'rejected', 'withdrawn', 'expired', 'applying', 'paused', "
    "'cancelled', 'finished', 'rolling_back', 'rolled_back'"
)
ITEM_STATUSES = (
    "'pending', 'writing', 'applied', 'skipped', 'out_of_date', 'manual', 'failed', 'cancelled'"
)
ROLLBACK_STATUSES = "'rolled_back', 'not_rolled_back', 'failed'"

TRANSITION_FUNCTION = """
CREATE OR REPLACE FUNCTION baluarte_template_change_set_transition() RETURNS trigger
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
    IF OLD.apply_started_at IS NOT NULL AND
       (NEW.applied_by_user_id, NEW.apply_started_at)
       IS DISTINCT FROM (OLD.applied_by_user_id, OLD.apply_started_at) THEN
        RAISE EXCEPTION 'who started applying a change set cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.cancelled_at IS NOT NULL AND
       (NEW.cancelled_by_user_id, NEW.cancelled_at)
       IS DISTINCT FROM (OLD.cancelled_by_user_id, OLD.cancelled_at) THEN
        RAISE EXCEPTION 'who cancelled a change set cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.rollback_started_at IS NOT NULL AND
       (NEW.rollback_by_user_id, NEW.rollback_started_at)
       IS DISTINCT FROM (OLD.rollback_by_user_id, OLD.rollback_started_at) THEN
        RAISE EXCEPTION 'who started rolling back a change set cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.stage_targets < OLD.stage_targets THEN
        RAISE EXCEPTION 'a change set stage cannot shrink'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = NEW.state AND OLD.state IN
       ('applying', 'paused', 'cancelled', 'finished', 'rolling_back', 'rolled_back') THEN
        RETURN NEW;
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
    IF OLD.state = 'approved' AND NEW.state = 'applying' THEN
        IF OLD.expires_at <= now() THEN
            RAISE EXCEPTION 'an expired template change set cannot be applied'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (OLD.state = 'applying' AND NEW.state IN ('paused', 'finished', 'cancelled'))
       OR (OLD.state = 'paused' AND NEW.state IN ('applying', 'cancelled'))
       OR (OLD.state IN ('paused', 'cancelled', 'finished') AND NEW.state = 'rolling_back')
       OR (OLD.state = 'rolling_back' AND NEW.state = 'rolled_back') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'template change set cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""

ITEM_FUNCTION = """
CREATE FUNCTION baluarte_template_change_item_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'template_change_item rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.change_set_id, NEW.position, NEW.target_index, NEW.project_id,
        NEW.project_gitlab_id, NEW.project_path, NEW.setting, NEW.before, NEW.after,
        NEW.irreversible)
       IS DISTINCT FROM
       (OLD.id, OLD.change_set_id, OLD.position, OLD.target_index, OLD.project_id,
        OLD.project_gitlab_id, OLD.project_path, OLD.setting, OLD.before, OLD.after,
        OLD.irreversible) THEN
        RAISE EXCEPTION 'what a change item approved cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _ck(name: str, condition: str) -> None:
    op.create_check_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, condition)


def upgrade() -> None:
    _names((*SETTING_KEYS, TOKEN_NAME), (*SECRET_NAMES, TOKEN_NAME))
    op.add_column(TABLE, sa.Column("applied_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("apply_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        TABLE, sa.Column("stage_targets", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(TABLE, sa.Column("runs", sa.Integer(), server_default="0", nullable=False))
    op.add_column(TABLE, sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("cancelled_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("rollback_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column(
        TABLE, sa.Column("rollback_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(TABLE, sa.Column("halt_reason", sa.Text(), nullable=True))
    for column in ("applied_by_user_id", "cancelled_by_user_id", "rollback_by_user_id"):
        op.create_foreign_key(
            op.f(f"fk_{TABLE}_{column}_app_user"), TABLE, "app_user", [column], ["id"]
        )
    op.drop_constraint(op.f(f"ck_{TABLE}_state_allowed"), TABLE, type_="check")
    _ck("state_allowed", f"state IN ({NEW_STATES})")
    op.drop_constraint(op.f(f"ck_{TABLE}_decided_has_decision"), TABLE, type_="check")
    _ck(
        "decided_has_decision",
        "state IN ('proposed', 'withdrawn') OR decided_at IS NOT NULL "
        "OR (state = 'expired' AND approved_digest IS NULL)",
    )
    op.drop_constraint(op.f(f"ck_{TABLE}_approved_has_digest"), TABLE, type_="check")
    _ck(
        "approved_has_digest",
        "state IN ('proposed', 'rejected', 'withdrawn', 'expired') OR approved_digest IS NOT NULL",
    )
    _ck(
        "applying_has_start",
        "state IN ('proposed', 'approved', 'rejected', 'withdrawn', 'expired') "
        "OR (apply_started_at IS NOT NULL AND applied_by_user_id IS NOT NULL "
        "AND stage_targets >= 1 AND runs >= 1)",
    )
    _ck("stage_bounded", "stage_targets BETWEEN 0 AND targets")
    _ck(
        "finished_has_time",
        "(state IN ('finished', 'rolled_back')) = (finished_at IS NOT NULL) "
        "OR state = 'rolling_back'",
    )
    _ck(
        "cancelled_has_user",
        "(cancelled_at IS NULL) = (cancelled_by_user_id IS NULL) "
        "AND (state <> 'cancelled' OR cancelled_at IS NOT NULL)",
    )
    _ck(
        "rollback_has_start",
        "(state IN ('rolling_back', 'rolled_back')) = (rollback_started_at IS NOT NULL) "
        "AND (rollback_started_at IS NULL) = (rollback_by_user_id IS NULL)",
    )
    op.execute(TRANSITION_FUNCTION)

    item = "template_change_item"
    op.create_table(
        item,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("change_set_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("target_index", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("project_gitlab_id", sa.BigInteger(), nullable=False),
        sa.Column("project_path", sa.Text(), nullable=False),
        sa.Column("setting", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("irreversible", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("request_status", sa.Integer(), nullable=True),
        sa.Column("previous", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("observed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_status", sa.Text(), nullable=True),
        sa.Column("rollback_message", sa.Text(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({ITEM_STATUSES})", name=op.f(f"ck_{item}_status_allowed")),
        sa.CheckConstraint(
            f"rollback_status IS NULL OR rollback_status IN ({ROLLBACK_STATUSES})",
            name=op.f(f"ck_{item}_rollback_status_allowed"),
        ),
        sa.CheckConstraint(
            "position >= 0 AND target_index >= 0 AND attempts >= 0",
            name=op.f(f"ck_{item}_non_negative"),
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'writing')) = (finished_at IS NULL)",
            name=op.f(f"ck_{item}_finished_has_time"),
        ),
        sa.CheckConstraint(
            "(rollback_status IS NULL) = (rolled_back_at IS NULL)",
            name=op.f(f"ck_{item}_rollback_has_time"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.id"], name=op.f(f"fk_{item}_workspace_id_workspace")
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["template_change_set.id"],
            name=op.f(f"fk_{item}_change_set_id_template_change_set"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["gitlab_project.id"], name=op.f(f"fk_{item}_project_id_gitlab_project")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{item}")),
        sa.UniqueConstraint(
            "change_set_id", "position", name=op.f(f"uq_{item}_change_set_id_position")
        ),
    )
    op.create_index(op.f(f"ix_{item}_workspace_id"), item, ["workspace_id"])
    op.execute(ITEM_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {item}_guard BEFORE UPDATE OR DELETE ON {item} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_template_change_item_guard()"
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM app_secret WHERE name = :name").bindparams(name=TOKEN_NAME))
    op.execute(sa.text("DELETE FROM app_setting WHERE key = :name").bindparams(name=TOKEN_NAME))
    _names(SETTING_KEYS, SECRET_NAMES)
    op.execute("DROP TRIGGER template_change_item_guard ON template_change_item")
    op.drop_table("template_change_item")
    op.execute("DROP FUNCTION baluarte_template_change_item_guard()")
    for name in (
        "rollback_has_start",
        "cancelled_has_user",
        "finished_has_time",
        "stage_bounded",
        "applying_has_start",
        "approved_has_digest",
        "decided_has_decision",
        "state_allowed",
    ):
        op.drop_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, type_="check")
    _ck("state_allowed", f"state IN ({OLD_STATES})")
    _ck("decided_has_decision", "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL")
    _ck("approved_has_digest", "state <> 'approved' OR approved_digest IS NOT NULL")
    for column in ("applied_by_user_id", "cancelled_by_user_id", "rollback_by_user_id"):
        op.drop_constraint(op.f(f"fk_{TABLE}_{column}_app_user"), TABLE, type_="foreignkey")
    for column in (
        "halt_reason",
        "rollback_started_at",
        "rollback_by_user_id",
        "cancelled_at",
        "cancelled_by_user_id",
        "finished_at",
        "runs",
        "stage_targets",
        "apply_started_at",
        "applied_by_user_id",
    ):
        op.drop_column(TABLE, column)
    # The transition function stays as upgraded: it only adds transitions into states that the
    # restored state check no longer allows.
