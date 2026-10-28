"""ASGI application entry point."""

from fastapi import FastAPI

from baluarte import __version__
from baluarte.api import v1


def create_app() -> FastAPI:
    app = FastAPI(
        title="Baluarte",
        version=__version__,
        description="Governance and audit evidence for self-managed GitLab.",
    )
    app.include_router(v1.router)
    return app


app = create_app()
