"""Framework versions as data (roadmap M15.1, docs/EVIDENCE_MANAGEMENT.md).

- ``framework_version``: each edition of a framework, with its publisher, a licence note and
  whether the catalogue may carry the publisher's control text. One version per framework is
  ``current``; a new version is added beside the old one, never in place of it.
- ``framework_control``: the controls of a version, with Baluarte's paraphrased titles.
- ``framework_control_description``: an organization's own description of a control, for its
  own use; the latest row is in force and none is ever changed or deleted.
- ``control_mapping_change`` and ``issue_control_mapping`` gain ``framework_version_id``: the
  version they were made against. Existing rows take the seeded version of their framework.

Catalogue rows change only in ``framework_version.current``, and nothing is deleted (triggers).

Revision ID: 0050
Revises: 0049
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FRAMEWORKS = ("soc2", "iso27001", "nist_800_53_moderate")
PINNED = ("control_mapping_change", "issue_control_mapping")

# The editions the built-in catalogue was written against (baluarte.controls.catalogue).
VERSIONS = (
    (
        "soc2",
        "2017-r2022",
        "AICPA Trust Services Criteria (2017, points of focus revised 2022)",
        "AICPA",
        "Copyright AICPA. Titles here are Baluarte's paraphrases, not the criteria's text.",
        False,
    ),
    (
        "iso27001",
        "2022",
        "ISO/IEC 27001:2022 Annex A",
        "ISO/IEC",
        "Copyright ISO. Titles here are Baluarte's paraphrases, not the standard's text.",
        False,
    ),
    (
        "nist_800_53_moderate",
        "rev5-moderate",
        "NIST SP 800-53 Rev. 5, moderate baseline",
        "NIST",
        "A work of the US government, not subject to copyright in the United States.",
        True,
    ),
)

CONTROLS = (
    ("soc2", "CC6.1", "Logical access to information assets is restricted", "Access control"),
    (
        "soc2",
        "CC6.2",
        "People are registered and authorized before they get access",
        "Access control",
    ),
    ("soc2", "CC6.3", "Access is granted, changed and removed by role", "Access control"),
    ("soc2", "CC6.6", "Access from outside the system boundary is restricted", "Access control"),
    (
        "soc2",
        "CC6.8",
        "Unauthorized or malicious software is prevented or detected",
        "Secure development",
    ),
    (
        "soc2",
        "CC7.1",
        "Configuration changes and vulnerabilities are detected",
        "Vulnerability management",
    ),
    (
        "soc2",
        "CC8.1",
        "Changes are authorized, tested and approved before release",
        "Change management",
    ),
    ("iso27001", "A.5.15", "Access rules are defined and enforced", "Access control"),
    ("iso27001", "A.5.17", "Authentication secrets are issued and managed", "Authentication"),
    ("iso27001", "A.5.18", "Access rights are granted, reviewed and removed", "Access control"),
    ("iso27001", "A.8.2", "Privileged access is limited and managed", "Access control"),
    ("iso27001", "A.8.3", "Access to information follows the access rules", "Access control"),
    ("iso27001", "A.8.4", "Read and write access to source code is controlled", "Access control"),
    (
        "iso27001",
        "A.8.5",
        "Authentication is strong enough for the access it grants",
        "Authentication",
    ),
    (
        "iso27001",
        "A.8.8",
        "Technical vulnerabilities are found and addressed",
        "Vulnerability management",
    ),
    (
        "iso27001",
        "A.8.9",
        "Configurations are defined, applied and monitored",
        "Configuration management",
    ),
    ("iso27001", "A.8.25", "Security is part of the development life cycle", "Secure development"),
    ("iso27001", "A.8.28", "Code is checked against secure coding practice", "Secure development"),
    (
        "iso27001",
        "A.8.29",
        "Security is tested during development and acceptance",
        "Secure development",
    ),
    ("iso27001", "A.8.32", "Changes follow a controlled process", "Change management"),
    (
        "nist_800_53_moderate",
        "AC-2",
        "Accounts are managed from creation to removal",
        "Access control",
    ),
    ("nist_800_53_moderate", "AC-3", "Approved authorizations are enforced", "Access control"),
    ("nist_800_53_moderate", "AC-5", "Duties are separated between people", "Access control"),
    ("nist_800_53_moderate", "AC-6", "Only the access a task needs is granted", "Access control"),
    (
        "nist_800_53_moderate",
        "AC-6(5)",
        "Privileged accounts are limited to designated people",
        "Access control",
    ),
    (
        "nist_800_53_moderate",
        "IA-2(1)",
        "Privileged accounts sign in with more than one factor",
        "Authentication",
    ),
    (
        "nist_800_53_moderate",
        "IA-5",
        "Authenticators are managed, including their lifetime",
        "Authentication",
    ),
    (
        "nist_800_53_moderate",
        "CM-2",
        "A baseline configuration is kept current",
        "Configuration management",
    ),
    (
        "nist_800_53_moderate",
        "CM-3",
        "Configuration changes are reviewed and approved",
        "Change management",
    ),
    (
        "nist_800_53_moderate",
        "CM-5",
        "Who can change the system is restricted",
        "Change management",
    ),
    (
        "nist_800_53_moderate",
        "CM-6",
        "Security settings are defined and enforced",
        "Configuration management",
    ),
    (
        "nist_800_53_moderate",
        "RA-5",
        "Systems are scanned for vulnerabilities",
        "Vulnerability management",
    ),
    (
        "nist_800_53_moderate",
        "SA-11",
        "Developers test and evaluate security",
        "Secure development",
    ),
    ("nist_800_53_moderate", "SI-2", "Flaws are found and corrected", "Vulnerability management"),
)

CATALOGUE_FIXED = """
CREATE FUNCTION baluarte_framework_catalogue_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'framework catalogue rows cannot be deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_TABLE_NAME = 'framework_version'
       AND (to_jsonb(NEW) - 'current') = (to_jsonb(OLD) - 'current') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% rows cannot change', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

APPEND_ONLY = """
CREATE FUNCTION baluarte_framework_control_description_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'a control description cannot be changed or deleted; add a new one'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

VERSION_PINNED = """
CREATE FUNCTION baluarte_framework_version_pinned() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.framework_version_id IS DISTINCT FROM OLD.framework_version_id THEN
        RAISE EXCEPTION 'the framework version a record was made against cannot change'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    framework_check = "framework IN ({})".format(", ".join(f"'{f}'" for f in FRAMEWORKS))
    op.create_table(
        "framework_version",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("framework", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("edition", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("licence_note", sa.Text(), nullable=False),
        sa.Column("text_redistributable", sa.Boolean(), nullable=False),
        sa.Column("current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_framework_version")),
        sa.UniqueConstraint(
            "framework", "version", name=op.f("uq_framework_version_framework_version")
        ),
        sa.CheckConstraint(framework_check, name=op.f("ck_framework_version_framework")),
        sa.CheckConstraint(
            "length(version) BETWEEN 1 AND 40", name=op.f("ck_framework_version_version_length")
        ),
    )
    op.create_index(
        "uq_framework_version_current",
        "framework_version",
        ["framework"],
        unique=True,
        postgresql_where=sa.text("current"),
    )
    op.create_table(
        "framework_control",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("framework_version_id", sa.BigInteger(), nullable=False),
        sa.Column("control_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("text_source", sa.Text(), server_default="baluarte", nullable=False),
        sa.ForeignKeyConstraint(
            ["framework_version_id"],
            ["framework_version.id"],
            name=op.f("fk_framework_control_framework_version_id_framework_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_framework_control")),
        sa.UniqueConstraint(
            "framework_version_id",
            "control_id",
            name=op.f("uq_framework_control_framework_version_id_control_id"),
        ),
        sa.CheckConstraint(
            "text_source IN ('baluarte', 'publisher')",
            name=op.f("ck_framework_control_text_source"),
        ),
    )
    op.create_table(
        "framework_control_description",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("framework_control_id", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("added_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_framework_control_description_workspace_id_workspace"),
        ),
        sa.ForeignKeyConstraint(
            ["framework_control_id"],
            ["framework_control.id"],
            name=op.f("fk_framework_control_description_framework_control_id_framework_control"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_framework_control_description")),
        sa.CheckConstraint(
            "length(description) BETWEEN 0 AND 4000",
            name=op.f("ck_framework_control_description_description_length"),
        ),
    )
    op.create_index(
        "ix_framework_control_description_control",
        "framework_control_description",
        ["workspace_id", "framework_control_id", "id"],
    )

    version = sa.table(
        "framework_version",
        sa.column("id", sa.BigInteger()),
        sa.column("framework", sa.Text()),
        sa.column("version", sa.Text()),
        sa.column("edition", sa.Text()),
        sa.column("publisher", sa.Text()),
        sa.column("licence_note", sa.Text()),
        sa.column("text_redistributable", sa.Boolean()),
        sa.column("current", sa.Boolean()),
    )
    op.bulk_insert(
        version,
        [
            {
                "framework": f,
                "version": v,
                "edition": edition,
                "publisher": publisher,
                "licence_note": note,
                "text_redistributable": free,
                "current": True,
            }
            for f, v, edition, publisher, note, free in VERSIONS
        ],
    )
    connection = op.get_bind()
    ids: dict[str, int] = dict(
        connection.execute(sa.text("SELECT framework, id FROM framework_version")).all()
    )
    control = sa.table(
        "framework_control",
        sa.column("framework_version_id", sa.BigInteger()),
        sa.column("control_id", sa.Text()),
        sa.column("title", sa.Text()),
        sa.column("area", sa.Text()),
    )
    op.bulk_insert(
        control,
        [
            {"framework_version_id": ids[f], "control_id": c, "title": title, "area": area}
            for f, c, title, area in CONTROLS
        ],
    )

    for table in PINNED:
        op.add_column(table, sa.Column("framework_version_id", sa.BigInteger(), nullable=True))
        op.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        backfill = (
            f"UPDATE {table} t SET framework_version_id = v.id FROM framework_version v "  # noqa: S608
            "WHERE v.framework = t.framework AND v.current"
        )
        op.execute(backfill)
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        op.alter_column(table, "framework_version_id", nullable=False)
        op.create_foreign_key(
            op.f(f"fk_{table}_framework_version_id_framework_version"),
            table,
            "framework_version",
            ["framework_version_id"],
            ["id"],
        )

    op.execute(CATALOGUE_FIXED)
    for table in ("framework_version", "framework_control"):
        op.execute(
            f"CREATE TRIGGER {table}_fixed BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION baluarte_framework_catalogue_fixed()"
        )
    op.execute(APPEND_ONLY)
    op.execute(
        "CREATE TRIGGER framework_control_description_fixed BEFORE UPDATE OR DELETE "
        "ON framework_control_description "
        "FOR EACH ROW EXECUTE FUNCTION baluarte_framework_control_description_fixed()"
    )
    op.execute(VERSION_PINNED)
    for table in PINNED:
        op.execute(
            f"CREATE TRIGGER {table}_framework_version_pinned BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION baluarte_framework_version_pinned()"
        )


def downgrade() -> None:
    for table in PINNED:
        op.execute(f"DROP TRIGGER {table}_framework_version_pinned ON {table}")
        op.drop_constraint(
            op.f(f"fk_{table}_framework_version_id_framework_version"), table, type_="foreignkey"
        )
        op.drop_column(table, "framework_version_id")
    op.execute("DROP FUNCTION baluarte_framework_version_pinned()")
    op.drop_table("framework_control_description")
    op.execute("DROP FUNCTION baluarte_framework_control_description_fixed()")
    op.drop_table("framework_control")
    op.drop_table("framework_version")
    op.execute("DROP FUNCTION baluarte_framework_catalogue_fixed()")
