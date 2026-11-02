"""Job handlers by kind.

A handler receives the claimed job and the worker's database engine. It must be idempotent: it
can run more than once for the same job. Raise JobError to retry with a stored message, or
PermanentJobError to stop retrying. Any other exception is retried and recorded by type name
only, since its text may hold secrets. Open short sessions from the engine; do not hold a
transaction open across GitLab calls.
"""

from collections.abc import Callable, Mapping

from sqlalchemy import Engine

from baluarte.compliance.retention import RETENTION_EXPIRE, handle_expire
from baluarte.integrations.gitlab.jobs import (
    DETECT_CAPABILITIES,
    DISCOVER_GROUPS,
    DISCOVER_PROJECTS,
    DISCOVER_SECURITY_POLICIES,
    make_detect_capabilities,
    make_discover_groups,
    make_discover_projects,
    make_discover_security_policies,
)
from baluarte.jobs.queue import ClaimedJob
from baluarte.policy.evaluation.runner import EVALUATE, evaluate

Handler = Callable[[ClaimedJob, Engine], None]

HANDLERS: Mapping[str, Handler] = {
    DETECT_CAPABILITIES: make_detect_capabilities(),
    DISCOVER_GROUPS: make_discover_groups(),
    DISCOVER_PROJECTS: make_discover_projects(),
    DISCOVER_SECURITY_POLICIES: make_discover_security_policies(),
    EVALUATE: evaluate,
    RETENTION_EXPIRE: handle_expire,
}
