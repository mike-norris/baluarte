"""Liveness endpoint.

Reports only that the process is serving requests. It does not check PostgreSQL or GitLab,
so a database outage does not restart the container; see /api/v1/ready for that.
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
