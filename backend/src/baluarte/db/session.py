from functools import lru_cache

from sqlalchemy import Engine, create_engine

from baluarte.config import database_url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine. Raises ConfigurationError if the database is not configured."""
    return create_engine(
        database_url(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
