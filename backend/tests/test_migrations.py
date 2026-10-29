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
    "app_user",
    "audit_event",
    "collection_job",
    "collection_run",
    "evaluation",
    "export_job",
    "finding",
    "gitlab_group",
    "gitlab_instance",
    "gitlab_project",
    "observation",
    "oidc_login",
    "policy",
    "policy_assignment",
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
            text("SELECT count(*) FROM pg_proc WHERE proname = 'baluarte_reject_modification'")
        ).scalar_one()
    assert functions == 0

    upgrade(migrated_engine)

    assert set(inspect(migrated_engine).get_table_names()) == TABLES
