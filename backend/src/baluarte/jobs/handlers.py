"""Job handlers by kind.

A handler receives the claimed job and the worker's database engine. It must be idempotent: it
can run more than once for the same job. Raise JobError to retry with a stored message, or
PermanentJobError to stop retrying. Any other exception is retried and recorded by type name
only, since its text may hold secrets. Open short sessions from the engine; do not hold a
transaction open across GitLab calls.
"""

from collections.abc import Callable, Mapping

from sqlalchemy import Engine

from baluarte.integrations.gitlab.jobs import DETECT_CAPABILITIES, make_detect_capabilities
from baluarte.jobs.queue import ClaimedJob

Handler = Callable[[ClaimedJob, Engine], None]

HANDLERS: Mapping[str, Handler] = {
    DETECT_CAPABILITIES: make_detect_capabilities(),
}
