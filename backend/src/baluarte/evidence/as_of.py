"""Reading the stored evidence as it stood at a past moment (FR-017, docs/EVALUATION.md).

Evaluations, observations and the review tables are kept with their timestamps, so a past view
is built from them without data recorded later:

- The evaluation run is the latest successful one that finished at or before ``as_of``; with
  none, the view is empty.
- A finding exists from its first failure (``first_seen_at``). Its state then follows its last
  decided evaluation by then: ``fail`` open, ``excepted`` excepted (open if that exception was no
  longer in force at ``as_of``), ``pass`` resolved as ``passed``. A finding the run did not
  evaluate was resolved: ``project_excluded`` when its project was out of monitoring scope then,
  otherwise the resolution it was later given if that happened by then, else
  ``no_longer_applies``. ``unknown`` and ``unsupported`` change no state, as they do live.
- Monitoring scope and the control mapping are the exclusions and changes in force then.

Inventory rows (paths, states) are today's: the inventory keeps no history of them. A finding's
severity is its current one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte.db.models import Evaluation, EvaluationRun, Finding, GitLabProject, PolicyException
from baluarte.scope.exclusions import Coverage

DECIDED = ("pass", "fail", "excepted")


class RunRef(BaseModel):
    """The evaluation run a view is read from."""

    id: int
    finished_at: datetime | None


def run_at(db: Session, instance_id: int | None, as_of: datetime | None) -> EvaluationRun | None:
    """The latest successful evaluation run; with ``as_of``, the latest finished by then."""
    statement = select(EvaluationRun).where(
        EvaluationRun.instance_id == (instance_id or 0), EvaluationRun.status == "succeeded"
    )
    if as_of is not None:
        statement = statement.where(EvaluationRun.finished_at <= as_of)
    return db.scalars(
        statement.order_by(EvaluationRun.finished_at.desc(), EvaluationRun.id.desc()).limit(1)
    ).first()


def run_ref(run: EvaluationRun | None) -> RunRef | None:
    return RunRef(id=run.id, finished_at=run.finished_at) if run is not None else None


def exception_in_force(exception: PolicyException | None, as_of: datetime) -> bool:
    """Whether an exception waived failures at ``as_of``."""
    if exception is None or exception.decided_at is None or exception.decided_at > as_of:
        return False
    if exception.state not in ("approved", "revoked", "expired"):
        return False
    if exception.expires_at <= as_of:
        return False
    return exception.ended_at is None or exception.ended_at > as_of


@dataclass(frozen=True)
class FindingAt:
    """A finding as it stood at ``as_of``."""

    state: str
    resolution: str | None
    # The latest evaluation of its rule by then.
    latest: Evaluation
    exception: PolicyException | None
    waived: bool


Key = tuple[int, str, str]


def _latest(
    db: Session, keys: Sequence[Key], as_of: datetime, outcomes: Sequence[str] | None = None
) -> dict[Key, Evaluation]:
    if not keys:
        return {}
    statement = select(Evaluation).where(
        tuple_(Evaluation.project_id, Evaluation.policy_name, Evaluation.rule_id).in_(keys),
        Evaluation.evaluated_at <= as_of,
    )
    if outcomes is not None:
        statement = statement.where(Evaluation.outcome.in_(outcomes))
    rows = db.scalars(
        statement.ext(
            distinct_on(Evaluation.project_id, Evaluation.policy_name, Evaluation.rule_id)
        ).order_by(
            Evaluation.project_id,
            Evaluation.policy_name,
            Evaluation.rule_id,
            Evaluation.evaluated_at.desc(),
            Evaluation.id.desc(),
        )
    )
    return {(row.project_id, row.policy_name, row.rule_id): row for row in rows}


def findings_at(
    db: Session,
    findings: Sequence[tuple[Finding, GitLabProject]],
    as_of: datetime,
    run: EvaluationRun,
    scope: Coverage,
) -> dict[int, FindingAt]:
    """The state at ``as_of`` of findings that existed then, by finding ID."""
    keys: list[Key] = [(f.project_id, f.policy_name, f.rule_id) for f, _ in findings]
    latest = _latest(db, keys, as_of)
    decided = _latest(db, keys, as_of, DECIDED)
    evaluated = (
        set(
            db.execute(
                select(Evaluation.project_id, Evaluation.policy_name, Evaluation.rule_id).where(
                    Evaluation.evaluation_run_id == run.id,
                    tuple_(Evaluation.project_id, Evaluation.policy_name, Evaluation.rule_id).in_(
                        keys
                    ),
                )
            ).all()
        )
        if keys
        else set()
    )
    exception_ids = {row.exception_id for row in decided.values() if row.exception_id is not None}
    exceptions = (
        {
            e.id: e
            for e in db.scalars(
                select(PolicyException).where(PolicyException.id.in_(exception_ids))
            )
        }
        if exception_ids
        else {}
    )
    found: dict[int, FindingAt] = {}
    for finding, project in findings:
        key = (finding.project_id, finding.policy_name, finding.rule_id)
        last = latest.get(key)
        if finding.first_seen_at > as_of or last is None:
            continue
        settled = decided.get(key)
        exception = (
            exceptions.get(settled.exception_id) if settled and settled.exception_id else None
        )
        waived = exception_in_force(exception, as_of)
        resolution: str | None = None
        if settled is None or settled.outcome == "fail":
            state = "open"
        elif settled.outcome == "excepted":
            state = "excepted" if waived else "open"
        else:
            state, resolution = "resolved", "passed"
        if state != "resolved" and key not in evaluated:
            state = "resolved"
            if scope.of(project) is not None:
                resolution = "project_excluded"
            elif (
                finding.resolved_at is not None
                and finding.resolved_at <= as_of
                and finding.resolution is not None
            ):
                resolution = finding.resolution
            else:
                resolution = "no_longer_applies"
        found[finding.id] = FindingAt(
            state=state,
            resolution=resolution,
            latest=last,
            exception=exception if state == "excepted" else None,
            waived=waived and state == "excepted",
        )
    return found
