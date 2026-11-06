import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from baluarte.db import models  # noqa: F401
from baluarte.db.base import Base
from baluarte.db.migrate import alembic_config, head_revisions, upgrade

pytestmark = pytest.mark.db

TABLES = {
    "alembic_version",
    "app_secret",
    "app_setting",
    "app_user",
    "audit_event",
    "audit_shipment",
    "backup_run",
    "change_cursor",
    "change_record",
    "collection_job",
    "collection_run",
    "compliance_attestation",
    "compliance_exception",
    "control_mapping_change",
    "evaluation_run",
    "evaluation",
    "export_job",
    "finding",
    "gitlab_group",
    "gitlab_instance",
    "gitlab_project",
    "gitlab_security_policy",
    "issue_estimate",
    "label_plan",
    "label_taxonomy",
    "manager_scope",
    "observation",
    "oidc_login",
    "policy",
    "policy_assignment",
    "policy_change",
    "policy_exception",
    "remediation_plan",
    "scope_exclusion",
    "setup_session",
    "setup_state",
    "tag",
    "tag_assignment",
    "time_cursor",
    "time_entry",
    "user_session",
}


def applied_revisions(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())


def test_upgrade_creates_schema_at_head(migrated_engine: Engine) -> None:
    assert set(inspect(migrated_engine).get_table_names()) == TABLES
    assert applied_revisions(migrated_engine) == head_revisions()


def test_models_match_migrations(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == []


def test_upgrade_is_idempotent(migrated_engine: Engine) -> None:
    upgrade(migrated_engine)

    assert applied_revisions(migrated_engine) == head_revisions()


def test_downgrade_to_base_and_upgrade_again(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, "base")

    assert set(inspect(migrated_engine).get_table_names()) == {"alembic_version"}
    with migrated_engine.connect() as connection:
        functions = connection.execute(
            text(
                "SELECT count(*) FROM pg_proc WHERE proname IN "
                "('baluarte_reject_modification', 'baluarte_policy_exception_transition', "
                "'baluarte_remediation_plan_transition', 'baluarte_policy_change_transition', "
                "'baluarte_compliance_exception_transition', 'baluarte_audit_event_immutable', "
                "'baluarte_expire_audit', 'baluarte_label_taxonomy_transition', "
                "'baluarte_label_plan_transition', 'baluarte_manager_scope_transition', "
                "'baluarte_tag_transition', 'baluarte_tag_assignment_transition')"
            )
        ).scalar_one()
    assert functions == 0

    upgrade(migrated_engine)

    assert set(inspect(migrated_engine).get_table_names()) == TABLES


def resolution_check(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_finding_resolution_allowed'"
            )
        ).scalar_one()


def test_0014_allows_project_archived_and_its_downgrade_keeps_existing_rows(
    migrated_engine: Engine,
) -> None:
    assert "'project_archived'" in resolution_check(migrated_engine)

    with migrated_engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, "0013")

    # NOT VALID: findings already resolved as project_archived stay; new ones are refused.
    downgraded = resolution_check(migrated_engine)
    assert "'project_archived'" not in downgraded
    assert "NOT VALID" in downgraded

    upgrade(migrated_engine)

    assert "'project_archived'" in resolution_check(migrated_engine)


def _migrate(engine: Engine, revision: str, *, down: bool = False) -> None:
    with engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        if down:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def test_0015_records_what_is_in_force_as_approved_and_migrated(empty_engine: Engine) -> None:
    _migrate(empty_engine, "0014")
    digest = "a" * 64
    with empty_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO gitlab_instance (id, base_url) VALUES (1, 'https://gitlab.test')")
        )
        connection.execute(
            text(
                "INSERT INTO policy (id, name, version, source_yaml, yaml_digest) "
                "VALUES (1, 'base', 1, 'v1', :d), (2, 'base', 2, 'v2', :d)"
            ),
            {"d": digest},
        )
        connection.execute(
            text(
                "INSERT INTO policy_assignment (id, instance_id, policy_id, policy_name, "
                "target_type, target_id) VALUES (7, 1, 2, 'base', 'instance', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO audit_event (actor, action, resource_type, resource_id, details, "
                "details_digest) VALUES ('user:5', 'policy.version_created', 'policy', "
                "'base@2', '{}', :d)"
            ),
            {"d": digest},
        )

    _migrate(empty_engine, "head")

    with empty_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT kind, state, policy_id, assignment_id, base_version, proposed_by, "
                "decided_by, review_required, self_approved, migrated "
                "FROM policy_change ORDER BY id"
            )
        ).all()
    migration = "system:migration-0015"
    assert [tuple(row) for row in rows] == [
        ("version", "approved", 1, None, None, "unknown", migration, False, False, True),
        ("version", "approved", 2, None, 1, "user:5", migration, False, False, True),
        ("assign", "approved", 2, 7, None, "unknown", migration, False, False, True),
    ]

    _migrate(empty_engine, "0014", down=True)

    assert "policy_change" not in inspect(empty_engine).get_table_names()
    with pytest.raises(Exception, match="ck_app_setting_key_allowed"):
        with empty_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO app_setting (key, value, updated_by) "
                    "VALUES ('governance', '{}', 'test')"
                )
            )
