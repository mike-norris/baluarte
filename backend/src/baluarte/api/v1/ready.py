"""Readiness endpoint.

Ready means the database is reachable and fully migrated. Failure details are logged, not
returned, so the response never exposes hosts, users or driver messages.
"""

import logging
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError

from baluarte.config import ConfigurationError
from baluarte.db.migrate import head_revisions
from baluarte.db.session import get_engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class ReadinessChecks(BaseModel):
    database: Literal["ok", "unavailable", "not_configured"]
    migrations: Literal["ok", "pending", "unknown"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


def optional_engine() -> Engine | None:
    try:
        return get_engine()
    except ConfigurationError as exc:
        logger.warning("readiness: %s", exc)
        return None


@lru_cache(maxsize=1)
def _heads() -> frozenset[str]:
    return frozenset(head_revisions())


def check_readiness(engine: Engine | None) -> ReadinessChecks:
    if engine is None:
        return ReadinessChecks(database="not_configured", migrations="unknown")
    try:
        with engine.connect() as connection:
            applied = set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
    except ProgrammingError:
        # Reachable, but alembic_version does not exist yet: never migrated.
        return ReadinessChecks(database="ok", migrations="pending")
    except SQLAlchemyError as exc:
        cause = exc.orig if isinstance(exc, DBAPIError) else exc
        logger.warning("readiness: database check failed (%s)", type(cause).__name__)
        return ReadinessChecks(database="unavailable", migrations="unknown")
    migrations: Literal["ok", "pending"] = "ok" if applied == _heads() else "pending"
    return ReadinessChecks(database="ok", migrations=migrations)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
    summary="Readiness check",
)
def ready(
    response: Response, engine: Annotated[Engine | None, Depends(optional_engine)]
) -> ReadinessResponse:
    checks = check_readiness(engine)
    is_ready = checks.database == "ok" and checks.migrations == "ok"
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if is_ready else "not_ready", checks=checks)
