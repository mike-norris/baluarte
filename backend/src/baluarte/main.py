"""ASGI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from baluarte import __version__
from baluarte.api import v1
from baluarte.settings import configure_logging_from_env

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Runs after uvicorn has configured its own loggers, so they are re-routed through ours.
    configure_logging_from_env()
    logger.info("baluarte %s API starting", __version__)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Baluarte",
        version=__version__,
        description="Governance and audit evidence for self-managed GitLab.",
        lifespan=lifespan,
    )
    app.include_router(v1.router)
    return app


app = create_app()
