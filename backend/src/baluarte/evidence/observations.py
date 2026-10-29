"""Observation-time evidence, stored in ``observation``.

An observation records the normalized GitLab data that evaluation uses for one target (a
project, a group, or the instance), the endpoint it came from, the collection run and job that
read it, the database time it was read, and the SHA-256 digest of its canonical JSON. A target
that could not be observed gets an observation too, with a status and a safe error but no
payload, so "could not observe" is itself evidence.

A new row is written only when the target's status, payload digest or error differs from its
latest observation from the same sources. A target can be observed through several sources (a
project's own fields, and its default-branch protection), and each is compared only with
observations from its own sources. Unchanged data is not copied every run: the latest
observation stays the evidence, and the resource's ``last_seen_at`` and the collection runs show
it was confirmed since. Rows are never updated (a trigger rejects ``UPDATE``), so an evaluation
that references an observation always points at exactly the data it judged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte.audit import details_digest
from baluarte.db.models import Observation

TargetColumn = Literal["project_id", "group_id"]
Fingerprint = tuple[str, str | None, str | None]


@dataclass(frozen=True)
class Provenance:
    """The collection run and job that read the data; None outside a job."""

    collection_run_id: int | None = None
    collection_job_id: int | None = None


@dataclass(frozen=True)
class Observed:
    """One target as read from GitLab. ``target_id`` is None for an instance-level observation."""

    target_id: int | None
    source: str
    source_ref: str | None
    collection_status: str
    payload: dict[str, Any] | None = None
    error: str | None = None


def payload_digest(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON form, the same algorithm as audit event details."""
    return details_digest(payload)


def verify(observation: Observation) -> bool:
    """True when the stored digest matches the stored payload (or both are absent)."""
    if observation.payload is None:
        return observation.payload_digest is None
    return observation.payload_digest == payload_digest(observation.payload)


def _target(column: TargetColumn | None, target_id: int | None) -> ColumnElement[bool]:
    if column is None:
        return and_(Observation.project_id.is_(None), Observation.group_id.is_(None))
    target: ColumnElement[bool] = getattr(Observation, column) == target_id
    return target


def latest(
    db: Session,
    instance_id: int,
    column: TargetColumn | None,
    target_id: int | None,
    source: str | None = None,
    *,
    sources: Sequence[str] | None = None,
) -> Observation | None:
    """The most recent observation of a target; for the instance, pass the ``source``.

    ``sources`` limits the search to observations from any of those sources, for example a
    project's own fields (``PROJECT_SOURCES``) rather than its branch protection.
    """
    statement = select(Observation).where(
        Observation.instance_id == instance_id, _target(column, target_id)
    )
    if source is not None:
        statement = statement.where(Observation.source == source)
    if sources is not None:
        statement = statement.where(Observation.source.in_(sources))
    return db.scalars(
        statement.order_by(Observation.collected_at.desc(), Observation.id.desc()).limit(1)
    ).first()


def _previous(
    db: Session,
    instance_id: int,
    column: TargetColumn | None,
    observed: Sequence[Observed],
    sources: Sequence[str],
) -> dict[int | None, Fingerprint]:
    if column is None:
        found: dict[int | None, Fingerprint] = {}
        for item in observed:
            row = latest(db, instance_id, None, None, item.source)
            if row is not None:
                found[None] = (row.collection_status, row.payload_digest, row.error)
        return found
    target = getattr(Observation, column)
    rows = db.execute(
        select(target, Observation.collection_status, Observation.payload_digest, Observation.error)
        .where(
            Observation.instance_id == instance_id,
            target.in_([item.target_id for item in observed]),
            Observation.source.in_(sources),
        )
        .ext(distinct_on(target))
        .order_by(target, Observation.collected_at.desc(), Observation.id.desc())
    )
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def record(
    db: Session,
    instance_id: int,
    column: TargetColumn | None,
    observed: Sequence[Observed],
    provenance: Provenance | None = None,
    compare_with: Sequence[str] | None = None,
) -> int:
    """Add the observations that differ from each target's latest one; returns how many.

    ``compare_with`` names the sources whose observations count as "the latest one"; by default,
    the sources of ``observed``. The caller commits them in the same transaction as the
    inventory change they describe.
    """
    if not observed:
        return 0
    provenance = provenance or Provenance()
    sources = compare_with if compare_with is not None else sorted({i.source for i in observed})
    previous = _previous(db, instance_id, column, observed, sources)
    added = 0
    for item in observed:
        digest = payload_digest(item.payload) if item.payload is not None else None
        if previous.get(item.target_id) == (item.collection_status, digest, item.error):
            continue
        db.add(
            Observation(
                instance_id=instance_id,
                project_id=item.target_id if column == "project_id" else None,
                group_id=item.target_id if column == "group_id" else None,
                collection_run_id=provenance.collection_run_id,
                collection_job_id=provenance.collection_job_id,
                source=item.source,
                source_ref=item.source_ref,
                collected_at=func.now(),
                collection_status=item.collection_status,
                payload=item.payload,
                payload_digest=digest,
                error=item.error,
            )
        )
        added += 1
    db.flush()
    return added
