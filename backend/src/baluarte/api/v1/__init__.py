"""Version 1 of the HTTP API, mounted at /api/v1.

Deny by default: routers included in ``protected`` require a signed-in session. Only probes and
the sign-in redirects are public.
"""

from fastapi import APIRouter, Depends

from baluarte.api.v1 import audit_events, auth, gitlab, health, jobs, ready, system
from baluarte.auth.dependencies import current_principal

public = APIRouter()
public.include_router(health.router)
public.include_router(ready.router)
public.include_router(auth.public_router)

protected = APIRouter(
    dependencies=[Depends(current_principal)],
    responses={
        401: {"description": "No valid session"},
        403: {"description": "Missing permission, or CSRF check failed on a state change"},
        503: {"description": "Authentication or the database is not configured"},
    },
)
protected.include_router(auth.router)
protected.include_router(jobs.router)
protected.include_router(audit_events.router)
protected.include_router(system.router)
protected.include_router(gitlab.router)

router = APIRouter(prefix="/api/v1")
router.include_router(public)
router.include_router(protected)
