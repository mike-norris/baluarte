"""Remediation execution: approved plans applied with a separate write token (BAL-032, ADR-0012).

``remediation_plan`` gains three states and the columns that record applying:

- ``approved`` -> ``applying`` when someone with ``remediation:plan`` applies it, before
  ``expires_at`` (``applied_by_user_id``, ``apply_started_at``);
- ``applying`` -> ``applied`` or ``failed`` (``finished_at``; ``failed_step`` and ``failure``
  say where and why it stopped);
- ``progress`` holds each step's outcome as it is applied: its status, the write's HTTP status
  and whether a read with the read token showed the intended value.

The trigger still keeps the plan, its digest and the decision unchanged, rejects ``DELETE`` and
allows only these transitions, plus progress updates while ``applying``. ``app_setting`` and
``app_secret`` accept ``remediation_write_token``.

A downgrade fails while any plan is applying, applied or failed, since the earlier state
constraint cannot hold for it.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-20 12:00:00.000000
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "remediation_plan"
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
)
SECRET_NAMES = (
    "oidc_client_secret",
    "gitlab_token",
    "audit_shipping_es_api_key",
    "audit_shipping_es_password",
    "label_write_token",
    "notifications_smtp_password",
    "notifications_webhook_url",
)
NEW = "remediation_write_token"

OLD_STATES = ("draft", "submitted", "approved", "rejected", "expired")
STATES = (*OLD_STATES, "applying", "applied", "failed")
APPLY = "('applying', 'applied', 'failed')"

# (name, old condition, new condition); the old ones are restored on downgrade.
CHANGED = (
    (
        "decided_has_decision",
        "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
        "state NOT IN ('approved', 'rejected', 'applying', 'applied', 'failed') "
        "OR decided_at IS NOT NULL",
    ),
    (
        "submitted_has_time",
        "state NOT IN ('submitted', 'approved', 'rejected') OR submitted_at IS NOT NULL",
        "state NOT IN ('submitted', 'approved', 'rejected', 'applying', 'applied', 'failed') "
        "OR submitted_at IS NOT NULL",
    ),
    (
        "approved_has_digest",
        "state <> 'approved' OR approved_digest IS NOT NULL",
        "state NOT IN ('approved', 'applying', 'applied', 'failed') OR approved_digest IS NOT NULL",
    ),
)
ADDED = (
    (
        "applying_has_start",
        f"state NOT IN {APPLY} "
        "OR (apply_started_at IS NOT NULL AND applied_by_user_id IS NOT NULL)",
    ),
    ("finished_has_time", "(state IN ('applied', 'failed')) = (finished_at IS NOT NULL)"),
    ("failure_explained", "(state = 'failed') = (failure IS NOT NULL)"),
)

FUNCTION = """
CREATE OR REPLACE FUNCTION baluarte_remediation_plan_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'remediation_plan rows cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.instance_id, NEW.created_by_user_id, NEW.created_at, NEW.expires_at,
        NEW.plan, NEW.plan_digest, NEW.steps, NEW.skipped)
       IS DISTINCT FROM
       (OLD.id, OLD.instance_id, OLD.created_by_user_id, OLD.created_at, OLD.expires_at,
        OLD.plan, OLD.plan_digest, OLD.steps, OLD.skipped) THEN
        RAISE EXCEPTION 'a remediation plan cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state IN ('approved', 'applying', 'applied', 'failed') AND
       (NEW.submitted_at, NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note,
        NEW.approved_digest, NEW.development_decision)
       IS DISTINCT FROM
       (OLD.submitted_at, OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note,
        OLD.approved_digest, OLD.development_decision) THEN
        RAISE EXCEPTION 'a remediation plan decision cannot change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'draft' AND NEW.state = 'submitted' THEN
        IF OLD.expires_at <= now() OR NEW.decided_at IS NOT NULL THEN
            RAISE EXCEPTION 'invalid remediation plan submission'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'submitted' AND NEW.state IN ('approved', 'rejected') THEN
        IF NEW.submitted_at IS DISTINCT FROM OLD.submitted_at OR OLD.decided_at IS NOT NULL THEN
            RAISE EXCEPTION 'invalid remediation plan decision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state = 'approved' AND OLD.expires_at <= now() THEN
            RAISE EXCEPTION 'an expired remediation plan cannot be approved'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state IN ('draft', 'submitted', 'approved') AND NEW.state = 'expired' THEN
        IF (NEW.submitted_at, NEW.decided_by_user_id, NEW.decided_at, NEW.decision_note,
            NEW.approved_digest)
           IS DISTINCT FROM
           (OLD.submitted_at, OLD.decided_by_user_id, OLD.decided_at, OLD.decision_note,
            OLD.approved_digest) THEN
            RAISE EXCEPTION 'a remediation plan decision cannot change'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.expires_at > now() THEN
            RAISE EXCEPTION 'a remediation plan cannot expire before expires_at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'approved' AND NEW.state = 'applying' THEN
        IF OLD.expires_at <= now() THEN
            RAISE EXCEPTION 'an expired remediation plan cannot be applied'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'applying' AND NEW.state IN ('applying', 'applied', 'failed') THEN
        IF (NEW.applied_by_user_id, NEW.apply_started_at)
           IS DISTINCT FROM (OLD.applied_by_user_id, OLD.apply_started_at) THEN
            RAISE EXCEPTION 'who applied a remediation plan, and when, cannot change'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'remediation plan cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _allowed(table: str, column: str, values: Sequence[str]) -> None:
    name = op.f(f"ck_{table}_{column}_allowed")
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"{column} IN ({_in(values)})")


def _replace(name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, type_="check")
    op.create_check_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, condition)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("applied_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("apply_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("failed_step", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("failure", sa.Text(), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        op.f(f"fk_{TABLE}_applied_by_user_id_app_user"),
        TABLE,
        "app_user",
        ["applied_by_user_id"],
        ["id"],
    )
    _allowed(TABLE, "state", STATES)
    for name, _old, new in CHANGED:
        _replace(name, new)
    for name, condition in ADDED:
        op.create_check_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, condition)
    op.execute(FUNCTION)
    _allowed("app_setting", "key", (*SETTING_KEYS, NEW))
    _allowed("app_secret", "name", (*SECRET_NAMES, NEW))


def _original_function() -> str:
    path = Path(__file__).with_name("0011_remediation_plans.py")
    spec = importlib.util.spec_from_file_location("remediation_plans_0011", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function: str = module.TRANSITION_FUNCTION
    return function.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def downgrade() -> None:
    op.execute("DELETE FROM app_secret WHERE name = 'remediation_write_token'")
    op.execute("DELETE FROM app_setting WHERE key = 'remediation_write_token'")
    _allowed("app_secret", "name", SECRET_NAMES)
    _allowed("app_setting", "key", SETTING_KEYS)
    op.execute(_original_function())
    for name, _condition in ADDED:
        op.drop_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, type_="check")
    for name, old, _new in CHANGED:
        _replace(name, old)
    _allowed(TABLE, "state", OLD_STATES)
    op.drop_constraint(op.f(f"fk_{TABLE}_applied_by_user_id_app_user"), TABLE, type_="foreignkey")
    for column in (
        "progress",
        "failure",
        "failed_step",
        "finished_at",
        "apply_started_at",
        "applied_by_user_id",
    ):
        op.drop_column(TABLE, column)
