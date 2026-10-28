from alembic import context
from sqlalchemy import create_engine

from baluarte.config import database_url
from baluarte.db import models  # noqa: F401  (registers tables on Base.metadata)
from baluarte.db.base import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url().render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # baluarte-migrate and the tests pass an open connection; the alembic CLI does not.
    connection = context.config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(database_url())
    with engine.begin() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
