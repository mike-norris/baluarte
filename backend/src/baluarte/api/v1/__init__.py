"""Version 1 of the HTTP API, mounted at /api/v1."""

from fastapi import APIRouter

from baluarte.api.v1 import health, ready

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(ready.router)
