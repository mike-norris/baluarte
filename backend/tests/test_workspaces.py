import pytest
from alembic import command
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from baluarte.db.migrate import alembic_config
from baluarte.db.models import GitLabInstance, Workspace
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.workspaces import DEFAULT_SLUG, connection_for_url, default_workspace_id

pytestmark = pytest.mark.db

URL = "https://gitlab.example.test"


def test_the_default_workspace_exists_after_migrating(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        workspace = db.scalars(select(Workspace)).one()

    assert (workspace.slug, workspace.name) == (DEFAULT_SLUG, "Default")


def test_a_new_connection_joins_the_default_workspace(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        connection = instance_for(db, URL)
        db.commit()

        assert connection.workspace_id == default_workspace_id(db)
        assert (connection.provider, connection.name, connection.paused) == ("gitlab", None, False)
        assert connection_for_url(db, URL) == connection
        assert connection_for_url(db, "https://other.example.test") is None


def test_default_workspace_id_is_stable(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        first = default_workspace_id(db)
        second = default_workspace_id(db)

        assert first == second
        assert db.scalars(select(Workspace)).all() == [db.get(Workspace, first)]


@pytest.mark.parametrize(
    ("column", "value"),
    [("provider", "bitbucket"), ("name", "")],
)
def test_connection_values_are_checked(migrated_engine: Engine, column: str, value: str) -> None:
    with Session(migrated_engine) as db:
        connection = GitLabInstance(base_url=URL, workspace_id=default_workspace_id(db))
        setattr(connection, column, value)
        db.add(connection)

        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize("slug", ["", "Upper", "-lead", "a" * 64, "with space"])
def test_workspace_slugs_are_checked(migrated_engine: Engine, slug: str) -> None:
    with Session(migrated_engine) as db:
        db.add(Workspace(slug=slug, name="A workspace"))

        with pytest.raises(IntegrityError, match="ck_workspace_slug_format"):
            db.commit()


def _migrate(engine: Engine, revision: str, *, down: bool = False) -> None:
    with engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        if down:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)


def test_0029_moves_existing_connections_into_the_default_workspace(
    empty_engine: Engine,
) -> None:
    _migrate(empty_engine, "0028")
    with empty_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO gitlab_instance (base_url) VALUES (:url)"), {"url": URL}
        )

    _migrate(empty_engine, "0029")

    with empty_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT w.slug, i.provider, i.paused FROM gitlab_instance i"
                " JOIN workspace w ON w.id = i.workspace_id"
            )
        ).one()
    assert tuple(row) == (DEFAULT_SLUG, "gitlab", False)

    _migrate(empty_engine, "0028", down=True)
    with empty_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM gitlab_instance")).scalar() == 1
