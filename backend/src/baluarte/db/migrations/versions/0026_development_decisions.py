"""Development decisions: in development mode the proposer may decide their own item.

Each reviewed item gains ``development_decision``, set when the person who proposed, requested
or created it approved or rejected it themselves, which only development mode allows
(``baluarte.settings.own_decision_refused``). The ``decided_by_another_user`` constraints accept
such a decision, a new ``development_decision_by_proposer`` constraint ties the flag to the
proposer, and a trigger lets the flag change only with the decision itself.

A downgrade fails while any item records a development decision, since the earlier constraints
cannot hold for it.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-13 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# table: (proposer column, whether the earlier constraint applied only while review was required)
TABLES = {
    "policy_exception": ("requested_by_user_id", False),
    "policy_change": ("proposed_by_user_id", True),
    "control_mapping_change": ("proposed_by_user_id", True),
    "scope_exclusion": ("proposed_by_user_id", True),
    "compliance_exception": ("requested_by_user_id", True),
    "remediation_plan": ("created_by_user_id", False),
    "label_taxonomy": ("proposed_by_user_id", True),
    "label_plan": ("created_by_user_id", True),
    "issue_plan": ("created_by_user_id", True),
}

FUNCTION = """
CREATE FUNCTION baluarte_development_decision_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.development_decision IS DISTINCT FROM OLD.development_decision
       AND NOT (OLD.decided_by_user_id IS NULL AND NEW.decided_by_user_id IS NOT NULL) THEN
        RAISE EXCEPTION 'a development decision is recorded only with the decision itself'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _another_user(proposer: str, reviewed: bool, development: bool) -> str:
    condition = f"decided_by_user_id IS NULL OR decided_by_user_id <> {proposer}"
    if reviewed:
        condition = f"NOT review_required OR {condition}"
    return f"{condition} OR development_decision" if development else condition


def upgrade() -> None:
    op.execute(FUNCTION)
    for table, (proposer, reviewed) in TABLES.items():
        op.add_column(
            table,
            sa.Column(
                "development_decision",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )
        op.drop_constraint(op.f(f"ck_{table}_decided_by_another_user"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_decided_by_another_user"),
            table,
            _another_user(proposer, reviewed, development=True),
        )
        op.create_check_constraint(
            op.f(f"ck_{table}_development_decision_by_proposer"),
            table,
            f"NOT development_decision OR decided_by_user_id = {proposer}",
        )
        op.execute(
            f"CREATE TRIGGER {table}_development_decision BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION baluarte_development_decision_fixed()"
        )


def downgrade() -> None:
    for table, (proposer, reviewed) in TABLES.items():
        op.execute(f"DROP TRIGGER {table}_development_decision ON {table}")
        op.drop_constraint(
            op.f(f"ck_{table}_development_decision_by_proposer"), table, type_="check"
        )
        op.drop_constraint(op.f(f"ck_{table}_decided_by_another_user"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_decided_by_another_user"),
            table,
            _another_user(proposer, reviewed, development=False),
        )
        op.drop_column(table, "development_decision")
    op.execute("DROP FUNCTION baluarte_development_decision_fixed()")
