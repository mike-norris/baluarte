"""Control assessments (roadmap M15.3, docs/EVIDENCE_MANAGEMENT.md#control-assessments-m153).

- ``control_assessment``: a person's conclusion about a control (pinned to its framework version)
  for a period, with a narrative; reviewed by someone other than the assessor.
- ``assessment_citation``: the evidence it cites, each with a rationale and a digest.

Triggers: a draft may change or be deleted; a submitted assessment may only be approved or
rejected; a decided one never changes. Citations change only while the assessment is a draft.

Revision ID: 0052
Revises: 0051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSESSMENT = "control_assessment"
CITATION = "assessment_citation"

ASSESSMENT_CHECKS = (
    (
        "conclusion_allowed",
        "conclusion IN ('effective', 'partially_effective', 'not_effective', 'not_tested')",
    ),
    ("state_allowed", "state IN ('draft', 'submitted', 'approved', 'rejected')"),
    ("narrative_length", "length(narrative) BETWEEN 20 AND 8000"),
    ("period_order", "period_end >= period_start"),
    (
        "reviewed_when_decided",
        "(state IN ('approved', 'rejected')) = (reviewed_at IS NOT NULL) "
        "AND (reviewed_at IS NULL) = (reviewed_by_user_id IS NULL)",
    ),
    ("submitted_unless_draft", "(state = 'draft') = (submitted_at IS NULL)"),
    (
        "reviewed_by_another_user",
        "reviewed_by_user_id IS NULL OR reviewed_by_user_id <> assessor_user_id "
        "OR development_review",
    ),
    (
        "development_review_by_assessor",
        "NOT development_review OR reviewed_by_user_id = assessor_user_id",
    ),
    ("rejection_explained", "state <> 'rejected' OR length(review_note) > 0"),
)
CITATION_CHECKS = (
    (
        "kind_allowed",
        "kind IN ('evidence_item', 'observation', 'change_record', 'change_set', 'control_status')",
    ),
    ("rationale_length", "length(rationale) BETWEEN 1 AND 2000"),
    (
        "one_target",
        "(kind = 'evidence_item') = (evidence_item_id IS NOT NULL) "
        "AND (kind = 'observation') = (observation_id IS NOT NULL) "
        "AND (kind = 'change_record') = (change_record_id IS NOT NULL) "
        "AND (kind = 'change_set') = (change_set_id IS NOT NULL) "
        "AND (kind = 'control_status') = (snapshot IS NOT NULL)",
    ),
)

# What a review never changes: the assessment as it was submitted.
CONTENT = (
    "id",
    "workspace_id",
    "framework_control_id",
    "period_start",
    "period_end",
    "conclusion",
    "narrative",
    "supersedes_id",
    "assessor",
    "assessor_user_id",
    "created_at",
    "submitted_at",
)


def _row(prefix: str, columns: Sequence[str]) -> str:
    return ", ".join(f"{prefix}.{column}" for column in columns)


TRANSITION = f"""
CREATE FUNCTION baluarte_control_assessment_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.state = 'draft' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'only a draft assessment can be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.workspace_id, NEW.assessor, NEW.assessor_user_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.workspace_id, OLD.assessor, OLD.assessor_user_id, OLD.created_at) THEN
        RAISE EXCEPTION 'who made an assessment cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'draft' THEN
        IF NEW.state IN ('draft', 'submitted') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'a draft assessment is submitted before it is reviewed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF ({_row("NEW", CONTENT)}) IS DISTINCT FROM ({_row("OLD", CONTENT)}) THEN
        RAISE EXCEPTION 'a submitted assessment cannot change; make a new one'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.state = 'submitted' AND NEW.state IN ('approved', 'rejected') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'an assessment cannot change from % to %', OLD.state, NEW.state
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

CITATION_DRAFT_ONLY = """
CREATE FUNCTION baluarte_assessment_citation_draft_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    assessment_state text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'an assessment''s citations are added or removed, never changed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    SELECT state INTO assessment_state FROM control_assessment
     WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.assessment_id ELSE NEW.assessment_id END;
    -- A deleted draft takes its citations with it (ON DELETE CASCADE).
    IF assessment_state IS NULL OR assessment_state = 'draft' THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    RAISE EXCEPTION 'evidence is cited only while an assessment is a draft'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        ASSESSMENT,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("framework_control_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="draft", nullable=False),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.Column("assessor", sa.Text(), nullable=False),
        sa.Column("assessor_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
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
            ["workspace_id"],
            ["workspace.id"],
            name=op.f(f"fk_{ASSESSMENT}_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["framework_control_id"],
            ["framework_control.id"],
            name=op.f(f"fk_{ASSESSMENT}_framework_control_id_framework_control"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            [f"{ASSESSMENT}.id"],
            name=op.f(f"fk_{ASSESSMENT}_supersedes_id_{ASSESSMENT}"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{ASSESSMENT}")),
        *(
            sa.CheckConstraint(rule, name=op.f(f"ck_{ASSESSMENT}_{name}"))
            for name, rule in ASSESSMENT_CHECKS
        ),
    )
    op.create_index(
        "ix_control_assessment_control", ASSESSMENT, ["workspace_id", "framework_control_id", "id"]
    )
    op.create_table(
        CITATION,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("assessment_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("evidence_item_id", sa.BigInteger(), nullable=True),
        sa.Column("observation_id", sa.BigInteger(), nullable=True),
        sa.Column("change_record_id", sa.BigInteger(), nullable=True),
        sa.Column("change_set_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("digest", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "cited_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            [f"{ASSESSMENT}.id"],
            name=op.f(f"fk_{CITATION}_assessment_id_{ASSESSMENT}"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_item_id"],
            ["evidence_item.id"],
            name=op.f(f"fk_{CITATION}_evidence_item_id_evidence_item"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f(f"fk_{CITATION}_observation_id_observation"),
        ),
        sa.ForeignKeyConstraint(
            ["change_record_id"],
            ["change_record.id"],
            name=op.f(f"fk_{CITATION}_change_record_id_change_record"),
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["template_change_set.id"],
            name=op.f(f"fk_{CITATION}_change_set_id_template_change_set"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{CITATION}")),
        *(
            sa.CheckConstraint(rule, name=op.f(f"ck_{CITATION}_{name}"))
            for name, rule in CITATION_CHECKS
        ),
    )
    op.create_index("ix_assessment_citation_assessment", CITATION, ["assessment_id", "id"])
    op.execute(TRANSITION)
    op.execute(
        f"CREATE TRIGGER {ASSESSMENT}_transition BEFORE UPDATE OR DELETE ON {ASSESSMENT} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_control_assessment_transition()"
    )
    op.execute(CITATION_DRAFT_ONLY)
    op.execute(
        f"CREATE TRIGGER {CITATION}_draft_only BEFORE INSERT OR UPDATE OR DELETE ON {CITATION} "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_assessment_citation_draft_only()"
    )


def downgrade() -> None:
    op.drop_table(CITATION)
    op.execute("DROP FUNCTION baluarte_assessment_citation_draft_only()")
    op.drop_table(ASSESSMENT)
    op.execute("DROP FUNCTION baluarte_control_assessment_transition()")
