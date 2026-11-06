"""Version 1 of the HTTP API, mounted at /api/v1.

Deny by default: routers included in ``protected`` require a signed-in session. Only probes, the
sign-in redirects and first-run setup are public; setup needs the one-time setup code instead.
"""

from fastapi import APIRouter, Depends

from baluarte.api.v1 import (
    audit_events,
    audit_shipping,
    auth,
    changes,
    compliance,
    controls,
    evaluations,
    exceptions,
    exports,
    gitlab,
    health,
    jobs,
    labels,
    manager,
    observations,
    policies,
    policy_assignments,
    policy_changes,
    ready,
    remediation,
    scope,
    security_policies,
    setup,
    system,
    tags,
    time,
)
from baluarte.auth.dependencies import current_principal

public = APIRouter()
public.include_router(health.router)
public.include_router(ready.router)
public.include_router(auth.public_router)
public.include_router(setup.router)

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
protected.include_router(audit_shipping.router)
protected.include_router(gitlab.router)
protected.include_router(scope.router)
protected.include_router(security_policies.router)
protected.include_router(observations.router)
protected.include_router(policies.router)
protected.include_router(policy_assignments.router)
protected.include_router(policy_changes.router)
protected.include_router(evaluations.router)
protected.include_router(changes.router)
protected.include_router(exceptions.router)
protected.include_router(exports.router)
protected.include_router(remediation.router)
protected.include_router(compliance.router)
protected.include_router(controls.router)
protected.include_router(labels.router)
protected.include_router(manager.router)
protected.include_router(tags.router)
protected.include_router(time.router)

router = APIRouter(prefix="/api/v1")
router.include_router(public)
router.include_router(protected)
