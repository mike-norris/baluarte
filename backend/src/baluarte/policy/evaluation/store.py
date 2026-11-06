"""Storing evaluation results and following findings (BAL-021).

Every result becomes an immutable ``evaluation`` row that names the observation it judged.
A ``pass`` or ``fail`` must be backed by the project's latest observation of the value's
source, whose SHA-256 digest still matches its payload, and whose payload matches the value
judged; otherwise it is stored as ``unknown`` (``evidence_missing``,
``evidence_digest_mismatch`` or ``evidence_mismatch``), so no stored compliance claim rests on
missing or altered evidence.

Findings follow a rule for a project by policy name, across versions:

- ``fail`` opens a finding, or reopens a resolved one.
- ``pass`` resolves an open finding (``resolution = passed``).
- ``unknown`` and ``unsupported`` change no state: they never resolve a finding and never open
  one. ``latest_outcome`` shows that the last check could not decide.
- A rule that no longer applies to the project resolves its open finding
  (``resolution = no_longer_applies``), but only when the project's policy resolution was
  complete: a gap in the group chain could be hiding the policy, so it proves nothing.
- An archived project is not evaluated at all: no evaluation rows, and its open and excepted
  findings are resolved (``resolution = project_archived``). Unarchived, it is judged again.
- Exceptions (BAL-033): a backed ``fail`` covered by an exception in force is stored as
  ``excepted`` with the exception's ID, keeping the observed value and evidence, and its
  finding becomes ``excepted`` (opened in that state if needed). Once no exception is in
  force, the next ``fail`` reopens it, a ``pass`` resolves it, and an ``unknown`` or
  ``unsupported`` result reopens it: a waived failure that can no longer be checked is not
  compliant. ``unknown`` and ``unsupported`` are never excepted.
"""

import logging
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.audit import details_digest
from baluarte.db.models import (
    ChangeRecord,
    Evaluation,
    Finding,
    GitLabProject,
    Observation,
    PolicyException,
)
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.changes import CHANGE_READ_SOURCE, parse_time
from baluarte.integrations.gitlab.labels import PROJECT_SOURCE as LABELS_SOURCE
from baluarte.integrations.gitlab.projects import PROJECT_SOURCES
from baluarte.integrations.gitlab.security_policies import PROJECT_SOURCE as SECURITY_POLICY_SOURCE
from baluarte.policy.assignments import Resolution
from baluarte.policy.evaluation import changes as change_rules
from baluarte.policy.evaluation import labels as label_rules
from baluarte.policy.evaluation.access import ACCESS_SOURCES
from baluarte.policy.evaluation.engine import ChangeEntry
from baluarte.policy.evaluation.model import RuleResult, UnknownReason
from baluarte.policy.evaluation.security_policy import summarize
from baluarte.policy.exceptions import active_for_project

logger = logging.getLogger(__name__)

# Sources of the project's own fields (listing and lookup), from discovery.
INVENTORY_SOURCES: tuple[str, ...] = tuple(PROJECT_SOURCES)


def _whole(payload: dict[str, Any], result: RuleResult) -> Any:
    return payload


def _visibility(payload: dict[str, Any], result: RuleResult) -> Any:
    return payload.get("visibility")


def _security_policies(payload: dict[str, Any], result: RuleResult) -> Any:
    # The judged value is a summary of the payload; rebuild it the same way.
    return summarize(result.rule_type, result.target.path, payload)


@dataclass(frozen=True)
class Evidence:
    """Which observations can back a result, and how to read the judged value from one."""

    sources: tuple[str, ...]
    # The value in an observation's payload, compared with the judged value. None when it is
    # not known, so a pass or fail cannot be backed.
    value: Callable[[dict[str, Any], RuleResult], Any] | None
    # Observation statuses that can back a value. A security policy observation is "partial"
    # when some other policy type was not read in full; the rule's own type is checked when it
    # is judged and again when its value is rebuilt here.
    statuses: tuple[str, ...] = ("ok",)


# Evidence by result ``source``. A rule type whose value lives elsewhere either adds an entry
# here or sets ``RuleResult.observation_source`` (its value is then the whole payload).
EVIDENCE: Mapping[str, Evidence] = {
    "gitlab_project.visibility": Evidence(INVENTORY_SOURCES, _visibility),
    # The default-branch protection rule judges the protection observation's payload itself.
    PROTECTION_SOURCE: Evidence((PROTECTION_SOURCE,), _whole),
    # Both GitLab security policy rules judge a summary of the project's policy observation.
    SECURITY_POLICY_SOURCE: Evidence(
        (SECURITY_POLICY_SOURCE,), _security_policies, ("ok", "partial")
    ),
    # The access-control rules each judge one observation's payload itself.
    **{source: Evidence((source,), _whole) for source in ACCESS_SOURCES},
}


# The change rules judge many change observations, read with the project's change-read
# observation; ``backed_by_changes`` checks them.
CHANGE_EVIDENCE = Evidence((CHANGE_READ_SOURCE,), None, ("ok", "partial"))


# The label rule judges the label observations of the project and the groups above it, read with
# the project's own label observation; ``backed_by_labels`` checks them.
LABEL_EVIDENCE = Evidence((LABELS_SOURCE,), None)


def evidence_for(result: RuleResult) -> Evidence:
    if result.rule_type in change_rules.CHANGE_RULE_TYPES:
        return CHANGE_EVIDENCE
    if result.rule_type == label_rules.RULE_TYPE:
        return LABEL_EVIDENCE
    found = EVIDENCE.get(result.source)
    if found is not None:
        return found
    if result.observation_source is not None:
        return Evidence((result.observation_source,), _whole)
    # An unknown source: link the project's own observation, but nothing says where its value
    # is, so a pass or fail is stored as unknown.
    return Evidence(INVENTORY_SOURCES, None)


@dataclass
class Tally:
    """Counts for an evaluation run."""

    outcomes: Counter[str] = field(default_factory=Counter)
    findings: Counter[str] = field(default_factory=Counter)

    def counts(self) -> dict[str, Any]:
        return {"outcomes": dict(self.outcomes), "findings": dict(self.findings)}


def _latest_observation(
    db: Session, instance_id: int, project_id: int, sources: Sequence[str]
) -> Observation | None:
    return db.scalars(
        select(Observation)
        .where(
            Observation.instance_id == instance_id,
            Observation.project_id == project_id,
            Observation.source.in_(sources),
        )
        .order_by(Observation.collected_at.desc(), Observation.id.desc())
        .limit(1)
    ).first()


def _unbacked(result: RuleResult, reason: UnknownReason, text: str) -> RuleResult:
    return result.model_copy(
        update={
            "outcome": "unknown",
            "observed": None,
            "observed_at": None,
            "unknown_reasons": (reason,),
            "reason": text,
        }
    )


def backed(result: RuleResult, evidence: Evidence, observation: Observation | None) -> RuleResult:
    """The result, or ``unknown`` when a pass or fail is not supported by the observation."""
    if result.outcome not in ("pass", "fail"):
        return result
    path = result.target.path
    if (
        observation is None
        or observation.collection_status not in evidence.statuses
        or observation.payload is None
    ):
        return _unbacked(
            result,
            "evidence_missing",
            f"No stored observation of project {path} backs this value; it will be evaluated "
            "again after the next project discovery records one.",
        )
    if not verify(observation):
        # The row was changed after it was written (the trigger forbids UPDATE, so only a
        # privileged change can do this). Never let it back a compliance claim.
        logger.warning(
            "observation %s fails its digest check; result for %s stored as unknown",
            observation.id,
            path,
        )
        return _unbacked(
            result,
            "evidence_digest_mismatch",
            f"The stored observation of project {path} fails its integrity check (its SHA-256 "
            "digest does not match its payload), so it cannot back this value.",
        )
    if evidence is CHANGE_EVIDENCE or evidence is LABEL_EVIDENCE:
        # Its value is checked against many observations (``backed_by_changes``,
        # ``backed_by_labels``).
        return result
    if evidence.value is None or evidence.value(observation.payload, result) != result.observed:
        return _unbacked(
            result,
            "evidence_mismatch",
            f"The latest stored observation of project {path} does not match the inventory, "
            "so the value cannot be backed by evidence.",
        )
    return result


def combined_digest(observations: Sequence[Observation]) -> str:
    """SHA-256 of the canonical JSON of ``[[id, payload_digest], ...]`` in ID order: one value that
    changes if any observation a result rests on is swapped, added, dropped or altered."""
    return details_digest(
        {
            "observations": [
                [o.id, o.payload_digest] for o in sorted(observations, key=lambda o: o.id)
            ]
        }
    )


@dataclass(frozen=True)
class ManyEvidence:
    """The change observations a change-rule result rests on, and their combined digest."""

    observation_ids: list[int]
    digest: str


def backed_by_changes(
    db: Session, project_id: int, result: RuleResult, read: Observation | None
) -> tuple[RuleResult, ManyEvidence | None]:
    """A change-rule result, or ``unknown`` when the stored observations do not support it.

    The read observation must back it like any single observation; then every change in the
    window is read again from the store, each observation's digest is checked, and the observed
    value is rebuilt from them and compared with the one judged.
    """
    result = backed(result, CHANGE_EVIDENCE, read)
    if result.outcome not in ("pass", "fail") or read is None or read.payload is None:
        return result, None
    observed = result.observed
    start, end = parse_time(observed.get("from")), parse_time(observed.get("to"))
    if start is None or end is None:
        return _unbacked(
            result, "evidence_mismatch", "The judged window of changes was not recorded."
        ), None
    rows = db.execute(
        select(ChangeRecord.id, ChangeRecord.kind, ChangeRecord.changed_at, Observation)
        .join(Observation, Observation.id == ChangeRecord.observation_id)
        .where(
            ChangeRecord.project_id == project_id,
            ChangeRecord.changed_at >= end - timedelta(days=change_rules.MAX_WINDOW_DAYS),
            ChangeRecord.changed_at <= end,
        )
        .order_by(ChangeRecord.changed_at, ChangeRecord.id)
    ).all()
    observations = {row[3].id: row[3] for row in rows}
    entries = [
        ChangeEntry(change_id, kind, changed_at, o.id, o.payload_digest, o.payload)
        for change_id, kind, changed_at, o in rows
    ]
    used = change_rules.evidence_entries(result.rule_type, read.payload, entries, (start, end))
    path = result.target.path
    altered = [
        entry.observation_id for entry in used if not verify(observations[entry.observation_id])
    ]
    if altered:
        logger.warning(
            "change observations %s fail their digest check; result for %s stored as unknown",
            altered[:10],
            path,
        )
        return _unbacked(
            result,
            "evidence_digest_mismatch",
            f"{len(altered)} stored change observations of project {path} fail their integrity "
            "check, so they cannot back this value.",
        ), None
    rebuilt = change_rules.summarise(
        result.rule_type, result.expected, read.payload, entries, (start, end)
    )
    if rebuilt != observed:
        return _unbacked(
            result,
            "evidence_mismatch",
            f"The stored change observations of project {path} do not match the judged value.",
        ), None
    chosen = [observations[entry.observation_id] for entry in used]
    return result, ManyEvidence([o.id for o in chosen], combined_digest(chosen))


def backed_by_labels(
    db: Session, result: RuleResult, read: Observation | None
) -> tuple[RuleResult, ManyEvidence | None]:
    """A label-rule result, or ``unknown`` when its label observations do not support it.

    The project's own label observation must back it like any single observation; then every
    label observation the drift was computed from must still be stored with the digest judged,
    and pass its integrity check.
    """
    result = backed(result, LABEL_EVIDENCE, read)
    if result.outcome not in ("pass", "fail"):
        return result, None
    listed = [(int(pair[0]), pair[1]) for pair in result.observed.get("observations", [])]
    if not listed:
        return result, None
    rows = {
        row.id: row
        for row in db.scalars(
            select(Observation).where(Observation.id.in_([obs_id for obs_id, _ in listed]))
        )
    }
    path = result.target.path
    for obs_id, digest in listed:
        row = rows.get(obs_id)
        if row is None or row.payload_digest != digest:
            return _unbacked(
                result,
                "evidence_mismatch",
                f"The label observations judged for project {path} are no longer stored as "
                "they were judged.",
            ), None
        if not verify(row):
            logger.warning(
                "label observation %s fails its digest check; result for %s stored as unknown",
                obs_id,
                path,
            )
            return _unbacked(
                result,
                "evidence_digest_mismatch",
                f"A stored label observation of project {path} fails its integrity check, so it "
                "cannot back this value.",
            ), None
    chosen = [rows[obs_id] for obs_id, _ in listed]
    return result, ManyEvidence(
        [o.id for o in sorted(chosen, key=lambda o: o.id)], combined_digest(chosen)
    )


def excepted(result: RuleResult, exception: PolicyException | None) -> RuleResult:
    """A backed ``fail`` waived by ``exception``; every other result unchanged."""
    if result.outcome != "fail" or exception is None:
        return result
    until = exception.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    return result.model_copy(
        update={
            "outcome": "excepted",
            "reason": f"{result.reason} Waived by approved exception {exception.id} until {until}.",
        }
    )


def _row(
    instance_id: int,
    project: GitLabProject,
    result: RuleResult,
    observation: Observation | None,
    run_id: int | None,
    exception_id: int | None,
    many: "ManyEvidence | None" = None,
) -> Evaluation:
    return Evaluation(
        instance_id=instance_id,
        project_id=project.id,
        evaluation_run_id=run_id,
        observation_id=observation.id if observation is not None else None,
        policy_id=result.policy.policy_id,
        policy_name=result.policy.name,
        rule_id=result.rule_id,
        rule_type=result.rule_type,
        severity=result.severity,
        outcome=result.outcome,
        reason=result.reason,
        unknown_reasons=list(result.unknown_reasons),
        expected=result.expected,
        observed=result.observed,
        source=result.source,
        observed_at=result.observed_at,
        capability_name=result.capability.name,
        capability_status=result.capability.status,
        collection_kind=result.collection.kind,
        collection_status=result.collection.status,
        collection_last_success_at=result.collection.last_success_at,
        ancestry=result.ancestry,
        exception_id=exception_id,
        evaluated_at=result.evaluated_at,
        observation_ids=many.observation_ids if many is not None else None,
        evidence_digest=many.digest if many is not None else None,
    )


def _follow(
    db: Session, project: GitLabProject, row: Evaluation, in_force: set[int], tally: Tally
) -> None:
    finding = db.scalars(
        select(Finding)
        .where(
            Finding.project_id == project.id,
            Finding.policy_name == row.policy_name,
            Finding.rule_id == row.rule_id,
        )
        .with_for_update()
    ).first()
    if finding is None:
        if row.outcome in ("fail", "excepted"):
            excepted = row.outcome == "excepted"
            db.add(
                Finding(
                    project_id=project.id,
                    policy_id=row.policy_id,
                    policy_name=row.policy_name,
                    rule_id=row.rule_id,
                    latest_evaluation_id=row.id,
                    latest_outcome=row.outcome,
                    severity=row.severity,
                    state="excepted" if excepted else "open",
                    exception_id=row.exception_id,
                    first_seen_at=row.evaluated_at,
                    last_seen_at=row.evaluated_at,
                )
            )
            tally.findings["excepted" if excepted else "opened"] += 1
        return
    finding.policy_id = row.policy_id
    finding.latest_evaluation_id = row.id
    finding.latest_outcome = row.outcome
    if row.outcome == "excepted":
        if finding.state != "excepted":
            tally.findings["excepted"] += 1
        finding.state = "excepted"
        finding.exception_id = row.exception_id
        finding.severity = row.severity
        finding.last_seen_at = row.evaluated_at
        finding.resolved_at = None
        finding.resolution = None
        return
    if finding.state == "excepted":
        if row.outcome in ("unknown", "unsupported") and finding.exception_id in in_force:
            # Still waived; the next decided result settles it.
            return
        # The exception ended (or the rule now passes): follow the finding as an open one.
        finding.state = "open"
        finding.exception_id = None
        tally.findings["exception_ended"] += 1
    if row.outcome == "fail":
        finding.severity = row.severity
        finding.last_seen_at = row.evaluated_at
        if finding.state == "resolved":
            finding.state = "open"
            finding.resolved_at = None
            finding.resolution = None
            tally.findings["reopened"] += 1
    elif row.outcome == "pass" and finding.state == "open":
        finding.state = "resolved"
        finding.resolved_at = row.evaluated_at
        finding.resolution = "passed"
        tally.findings["resolved"] += 1


def _resolve_all(
    db: Session,
    project: GitLabProject,
    resolution: str,
    tally: Tally,
    keep: Collection[tuple[str, str]] = (),
) -> None:
    """Resolve the project's open and excepted findings, except those in ``keep``."""
    open_findings = db.scalars(
        select(Finding)
        .where(Finding.project_id == project.id, Finding.state.in_(("open", "excepted")))
        .with_for_update()
    ).all()
    stale = [f for f in open_findings if (f.policy_name, f.rule_id) not in keep]
    if not stale:
        return
    now = db.execute(select(func.now())).scalar_one()
    for finding in stale:
        finding.exception_id = None
        finding.state = "resolved"
        finding.resolved_at = now
        finding.resolution = resolution
        tally.findings[resolution] += 1


def _close_inapplicable(
    db: Session, project: GitLabProject, applied: set[tuple[str, str]], tally: Tally
) -> None:
    _resolve_all(db, project, "no_longer_applies", tally, keep=applied)


def close_archived(db: Session, project: GitLabProject, tally: Tally) -> None:
    """An archived project is not evaluated: resolve its open and excepted findings.

    No evaluation rows are written. If the project is unarchived, the next evaluation judges it
    again and a ``fail`` reopens the finding. The caller commits.
    """
    _resolve_all(db, project, "project_archived", tally)


def close_excluded(db: Session, project: GitLabProject, tally: Tally) -> None:
    """A project out of monitoring is not evaluated: resolve its open and excepted findings.

    Like :func:`close_archived`: when it is included again, the next evaluation judges it and a
    ``fail`` reopens the finding. The caller commits.
    """
    _resolve_all(db, project, "project_excluded", tally)


def store_project(
    db: Session,
    instance_id: int,
    project: GitLabProject,
    resolution: Resolution,
    results: Sequence[RuleResult],
    run_id: int | None,
    tally: Tally,
) -> list[Evaluation]:
    """Store a project's results and update its findings. The caller commits."""
    observations: dict[tuple[str, ...], Observation | None] = {}
    exceptions = active_for_project(db, project.id)
    in_force = {exception.id for exception in exceptions.values()}
    rows: list[Evaluation] = []
    for result in results:
        evidence = evidence_for(result)
        if evidence.sources not in observations:
            observations[evidence.sources] = _latest_observation(
                db, instance_id, project.id, evidence.sources
            )
        observation = observations[evidence.sources]
        many: ManyEvidence | None = None
        if evidence is CHANGE_EVIDENCE:
            result, many = backed_by_changes(db, project.id, result, observation)
        elif evidence is LABEL_EVIDENCE:
            result, many = backed_by_labels(db, result, observation)
        else:
            result = backed(result, evidence, observation)
        exception = exceptions.get((result.policy.name, result.rule_id))
        result = excepted(result, exception)
        exception_id = exception.id if result.outcome == "excepted" and exception else None
        row = _row(instance_id, project, result, observation, run_id, exception_id, many)
        db.add(row)
        db.flush()
        _follow(db, project, row, in_force, tally)
        tally.outcomes[row.outcome] += 1
        rows.append(row)
    if resolution.ancestry == "complete":
        _close_inapplicable(db, project, {(row.policy_name, row.rule_id) for row in rows}, tally)
    return rows
