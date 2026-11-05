"""Per-project reads after project discovery: the evidence for project rules beyond the inventory.

Each active project is read by a list of readers, one observation per project and reader
(``baluarte.integrations.gitlab.branches`` for default-branch protection,
``baluarte.integrations.gitlab.access`` for merge request approvals, members, access tokens and
two-factor enforcement). A reader that does not apply to a project, such as protection for an
empty repository, records nothing for it.

Projects are read one at a time and results are written in small batches, so no database
transaction is open while GitLab is called. If GitLab rejects the token, every remaining read is
recorded as not read without calling GitLab. If one reader keeps failing, its remaining reads are
recorded as not read, so a stale observation never outlives a collection that did not confirm
it; the other readers carry on.
"""

import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabProject
from baluarte.evidence.observations import Observed, Provenance, record
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.scope.exclusions import coverage

logger = logging.getLogger(__name__)

# Stop a reader after this many of its reads in a row fail; the rest are recorded as not read.
MAX_CONSECUTIVE_FAILURES = 20
BATCH = 100
STOPPED_BY_REJECTION = "not read: GitLab rejected the token earlier in this collection"


@dataclass(frozen=True)
class ProjectTarget:
    """An active project to read, as the inventory records it."""

    row_id: int
    gitlab_id: int
    path: str
    default_branch: str | None
    namespace_gitlab_id: int
    namespace_kind: str | None
    archived: bool = False


class TokenRejected(Exception):
    """GitLab answered 401: the token no longer works, so reading other projects is pointless."""

    def __init__(self, observed: Observed) -> None:
        super().__init__(observed.error)
        self.observed = observed


# A read: the client, the project, and the observations already made for it in this pass by
# source (a reader may build on an earlier one). Raises TokenRejected on 401.
ReadFn = Callable[[GitLabClient, ProjectTarget, Mapping[str, Observed]], Observed]


@dataclass(frozen=True)
class Reader:
    # The name its counts are reported under, e.g. "approvals".
    key: str
    source: str
    # What a read is called in "N <noun> reads in a row failed".
    noun: str
    source_ref: Callable[[ProjectTarget], str | None]
    read: ReadFn
    applies: Callable[[ProjectTarget], bool] = lambda _: True
    # Statuses that count toward stopping the reader. Forbidden or not found is usually an answer
    # about one project (a role or a feature), so by default only failures do.
    stop_on: frozenset[str] = frozenset({"failed"})
    # The count an observation is reported under; its collection status by default.
    tally: Callable[[Observed], str] = lambda observed: observed.collection_status


@dataclass
class ProjectReads:
    """Counts per reader key, as each reader's ``tally`` names them."""

    counts: dict[str, Counter[str]] = field(default_factory=dict)

    def as_counts(self, key: str) -> dict[str, int]:
        return dict(sorted(self.counts.get(key, Counter()).items()))


def unread(reader: Reader, target: ProjectTarget, status: str, error: str) -> Observed:
    return Observed(
        target_id=target.row_id,
        source=reader.source,
        source_ref=reader.source_ref(target),
        collection_status=status,
        error=error,
    )


def targets(engine: Engine, instance_id: int) -> list[ProjectTarget]:
    """Active projects in monitoring scope; excluded projects are not read (``baluarte.scope``)."""
    with Session(engine) as db:
        scope = coverage(db, instance_id)
        rows = db.execute(
            select(
                GitLabProject.id,
                GitLabProject.gitlab_id,
                GitLabProject.path_with_namespace,
                GitLabProject.default_branch,
                GitLabProject.namespace_gitlab_id,
                GitLabProject.namespace_kind,
                GitLabProject.archived,
            )
            .where(
                GitLabProject.instance_id == instance_id,
                GitLabProject.state == "active",
                scope.project_included(),
            )
            .order_by(GitLabProject.id)
        ).all()
    return [ProjectTarget(*row) for row in rows]


def collect_project_reads(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    readers: Sequence[Reader],
    provenance: Provenance | None = None,
) -> ProjectReads:
    """Run every reader against every active project and record what each observed."""
    reads = ProjectReads({reader.key: Counter() for reader in readers})
    # One list per source: each source's observations are compared with its own latest ones.
    pending: dict[str, list[Observed]] = {reader.source: [] for reader in readers}
    stopped: dict[str, str] = {}
    failures: Counter[str] = Counter()
    unconfirmed: Counter[str] = Counter()
    rejected = False

    def flush() -> None:
        with Session(engine) as db:
            for observed in pending.values():
                record(db, instance_id, "project_id", observed, provenance)
                observed.clear()
            db.commit()

    for target in targets(engine, instance_id):
        done: dict[str, Observed] = {}
        for reader in readers:
            if not reader.applies(target):
                continue
            reason = STOPPED_BY_REJECTION if rejected else stopped.get(reader.key)
            if reason is not None:
                observed = unread(reader, target, "failed", reason)
            else:
                try:
                    observed = reader.read(client, target, done)
                except TokenRejected as error:
                    observed = error.observed
                    rejected = True
            done[reader.source] = observed
            reads.counts[reader.key][reader.tally(observed)] += 1
            if observed.collection_status in reader.stop_on:
                unconfirmed[reader.key] += 1
            if reason is None and observed.collection_status in reader.stop_on:
                failures[reader.key] += 1
                if failures[reader.key] >= MAX_CONSECUTIVE_FAILURES:
                    stopped[reader.key] = (
                        f"not read: {MAX_CONSECUTIVE_FAILURES} {reader.noun} reads in a row "
                        "failed earlier in this collection"
                    )
            elif reason is None:
                failures[reader.key] = 0
            pending[reader.source].append(observed)
        if sum(len(observed) for observed in pending.values()) >= BATCH:
            flush()
    flush()
    for reader in readers:
        if unconfirmed[reader.key]:
            logger.warning(
                "gitlab: %s reads failed for %s projects", reader.noun, unconfirmed[reader.key]
            )
    return reads
