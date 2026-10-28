"""Liveness endpoint.

Reports only that the process is serving requests. It does not check PostgreSQL or GitLab;
a separate readiness check is added once the database layer exists.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from baluarte import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)
