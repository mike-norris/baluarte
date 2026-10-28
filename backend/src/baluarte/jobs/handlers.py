"""Job handlers by kind. GitLab collection handlers register here from BAL-011 onwards.

A handler receives the claimed job and must be idempotent: it can run more than once for the
same job. Raise JobError to retry with a stored message, or PermanentJobError to stop retrying.
Any other exception is retried and recorded by type name only, since its text may hold secrets.
"""

from collections.abc import Callable, Mapping

from baluarte.jobs.queue import ClaimedJob

Handler = Callable[[ClaimedJob], None]

HANDLERS: Mapping[str, Handler] = {}
