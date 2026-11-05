"""The audit-period evidence package: one ZIP for a period an auditor examines (FR-018).

Built on the scoped export (:mod:`baluarte.evidence.export`), in the same read-only snapshot, with
the same manifest and ``SHA256SUMS``. What differs:

- Results are as of the period's end (``to``): the evaluations of the latest evaluation run that
  finished by then, findings in the state they were in then (:mod:`baluarte.evidence.as_of`), and
  the control mapping and monitoring scope in force then. Without such a run there is nothing to
  package (409 ``no_evaluation_run``).
- ``changes.csv``: every change that landed in the period (``from`` inclusive, ``to``
  exclusive) on a monitored project, with its stored outcome and current observation.
- ``change_sample.csv``: the sample of merge requests and direct pushes drawn with the package's
  seed (:mod:`baluarte.evidence.sampling`), so the same request gives the same sample.
- ``change_evidence.jsonl``: every observation recorded for each sampled change, oldest first,
  each with ``digest_valid`` recomputed now.
- The manifest's ``evidence_package`` member records the period, the evaluation run, the scope,
  the seed, the algorithm, the population size and the sampled IDs, and which projects' change
  history starts after the period does.

Packages are audited as ``evidence.package_created`` and ``evidence.package_completed``.
"""

import textwrap
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ColumnElement, Engine, func, or_, select
from sqlalchemy.orm import aliased

from baluarte.changes.records import history
from baluarte.compliance.catalogue import FrameworkId
from baluarte.controls.posture import effective as effective_mapping
from baluarte.controls.posture import mapping_summary
from baluarte.db.models import (
    ChangeCursor,
    ChangeRecord,
    Evaluation,
    Finding,
    GitLabGroup,
    GitLabProject,
    Observation,
    ScopeExclusion,
)
from baluarte.evidence import manifest, sampling
from baluarte.evidence.as_of import findings_at, run_at
from baluarte.evidence.export import (
    COLUMNS,
    PAGE,
    EvidenceExport,
    ExportScope,
    ScopeError,
    _dumps,
    _like_prefix,
    _safe,
    _ts,
)
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.logs import redact_value
from baluarte.scope.exclusions import coverage, justification_sha256

FORMAT = "baluarte.evidence-package/v1"
MAX_SAMPLE = 500
MAX_GROUPS = 50
# Projects whose change history starts after the period does, listed by path; beyond, a count.
MAX_LISTED = 100

PACKAGE_COLUMNS: dict[str, tuple[str, ...]] = {
    "changes": (
        "id",
        "kind",
        "project_gitlab_id",
        "project_path",
        "iid",
        "deployment_id",
        "sha",
        "title",
        "changed_at",
        "outcome",
        "reasons",
        "complete",
        "observation_id",
        "payload_digest",
        "sampled",
    ),
    "change_sample": (
        "position",
        "change_id",
        "kind",
        "project_path",
        "iid",
        "sha",
        "outcome",
    ),
}

LIMITATIONS = [
    "Evaluations, findings, the control mapping and monitoring scope are as of the period's end; "
    "changes are those that landed in the period, with the evidence recorded for them by the time "
    "the package was made (a deployment recorded after the period still shows).",
    "A change rule judges only the change history Baluarte collected: a project whose history "
    "starts after the period does has no evidence for the earlier part; evidence_package lists "
    "those projects.",
    "The sample is reproducible, not random in a security sense: anyone with the seed and the "
    "change IDs in changes.csv draws the same sample.",
]

README = """Baluarte evidence package
=========================

Export ID:     {export_id}
Generated at:  {generated_at} (database clock, UTC)
GitLab:        {instance}
Format:        {format}
Period:        {period_from} to {period_to} (from inclusive, to exclusive)
Results as of: {period_to}, from evaluation run {run_id} (finished {run_finished})
Scope:         {scope}
Sample:        {sample_size} of {population} changes, seed {seed}

Population
----------
{population_statement}

Files
-----
export.json            Package metadata: period, sample, scope and freshness at export time.
inventory.csv          Projects in scope, as the inventory holds them now. monitoring_excluded
                       marks projects out of monitoring scope at the period's end.
evaluations.csv        The results of the evaluation run in force at the period's end. A
                       change rule's result lists every observation it rests on
                       (observation_ids) and their combined evidence_digest.
findings.csv           Findings in the state they were in at the period's end.
changes.csv            Every change that landed in the period: merged merge requests, direct
                       pushes and production deployments, with their outcome under the
                       default checks (pass, fail or unknown) and sampled true when drawn.
change_sample.csv      The sampled merge requests and direct pushes, in ID order.
change_evidence.jsonl  Every observation recorded for each sampled change, oldest first: what
                       GitLab returned about its approvals, pipeline, tests and deployments.
observations.jsonl     The evidence the exported evaluations and findings reference.
policies.csv           The exact policy versions referenced, with their YAML and its digest.
exceptions.csv         Policy exceptions in force or pending at some point in the period.
collection_errors.csv  Projects and groups in scope that are missing, inaccessible or failed.
collection_runs.csv    Collection and evaluation runs started in the period.
manifest.json          Every file with its SHA-256 and row count, and evidence_package: the
                       period, evaluation run, seed, sampling algorithm and sampled IDs.
SHA256SUMS             SHA-256 of every file, including manifest.json.

Verifying
---------
1. Run baluarte-verify-export on the ZIP with --manifest-sha256 set to the value in the
   evidence.package_completed audit event. It checks every file, every observation digest in
   observations.jsonl and change_evidence.jsonl, and redraws the sample from the seed.
2. Without the tool: sha256sum -c SHA256SUMS in the extracted directory; recompute each
   payload_digest as the SHA-256 of the payload's canonical JSON (keys sorted, no whitespace,
   UTF-8); and redraw the sample in Python with
   sorted(random.Random(seed).sample(sorted(ids), size)), where ids are the changes.csv IDs of
   kind merge_request or direct_push.

Limitations
-----------
- unknown results are neither compliant nor failing: GitLab did not answer, or the token or
  edition cannot read it. A change with an unread part is unknown, never a pass.
- A rule evaluation is supporting evidence for an organization-defined control objective; it is
  not a SOC 2 certification, ISO 27001 certification, or NIST authorization.
- Digests show whether a row changed after it was written. They are not anchored outside the
  database, so a database superuser could rewrite a row and its digest together.
- Results come from the period's end; changes include evidence recorded later for them.
- The package covers only what the GitLab token can see; see Population above.
"""


class SampleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    size: int = Field(ge=1, le=MAX_SAMPLE, description="How many changes to sample.")
    seed: int | None = Field(
        None,
        ge=0,
        le=sampling.MAX_SEED,
        description="Draw with this seed to repeat an earlier sample; one is chosen when absent.",
    )


class PackageRequest(BaseModel):
    """The period, scope and sample of a package."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    period_from: AwareDatetime = Field(alias="from", description="Period start (inclusive).")
    period_to: AwareDatetime = Field(
        alias="to", description="Period end (exclusive); results are as of this moment."
    )
    framework: FrameworkId | None = None
    control_id: str | None = Field(None, min_length=1, max_length=20)
    groups: list[str] = Field(
        default_factory=list,
        max_length=MAX_GROUPS,
        description="Group full paths: projects in these groups and their subgroups.",
    )
    sample: SampleRequest

    @model_validator(mode="after")
    def _valid(self) -> "PackageRequest":
        if self.period_from >= self.period_to:
            raise ValueError("from must be before to")
        if (self.framework is None) != (self.control_id is None):
            raise ValueError("framework and control_id go together")
        for group in self.groups:
            if not group or group.strip("/") != group or len(group) > 255:
                raise ValueError("groups are full paths without leading or trailing slashes")
        if len({group.lower() for group in self.groups}) != len(self.groups):
            raise ValueError("groups must be distinct")
        return self

    def described(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude={"sample"}, exclude_none=True)


class PackageConflict(Exception):
    """The package cannot be built for this period."""

    def __init__(self, code: str, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reasons = reasons or []


class EvidencePackage(EvidenceExport):
    """One package, bound to a read-only snapshot. Call :meth:`plan`, then :meth:`stream` once."""

    FORMAT = FORMAT
    JSONL = ("observations", "change_evidence")
    PREFIX = "baluarte-evidence-package"

    def __init__(
        self,
        engine: Engine,
        settings: GitLabSettings,
        request: PackageRequest,
        export_id: str,
        seed: int,
        requested_by: str | None = None,
    ) -> None:
        scope = ExportScope.model_validate(
            {
                "framework": request.framework,
                "control_id": request.control_id,
                "since": request.period_from,
                "until": request.period_to,
            }
        )
        super().__init__(engine, settings, scope, "csv", export_id, requested_by=requested_by)
        self.request = request
        self.seed = seed
        self.to: datetime = request.period_to
        try:
            # Monitoring scope and the evaluation run as they stood at the period's end.
            self.coverage = coverage(self.db, self.instance_id, self.to)
            self.run = run_at(self.db, self.instance_id, self.to)
        except BaseException:
            self.db.close()
            raise
        self.population: list[int] = []
        self.sampled: list[int] = []

    # Scope ----------------------------------------------------------------------------------

    def _group_paths(self, column: Any) -> ColumnElement[bool]:
        path = func.lower(column)
        return or_(
            *(
                or_(path == group.lower(), path.like(_like_prefix(group), escape="\\"))
                for group in self.request.groups
            )
        )

    def _project_scope(self) -> list[ColumnElement[bool]]:
        conditions = super()._project_scope()
        if self.request.groups:
            conditions.append(self._group_paths(GitLabProject.path_with_namespace))
        return conditions

    def _group_scope(self) -> list[ColumnElement[bool]] | None:
        conditions = super()._group_scope()
        if conditions is not None and self.request.groups:
            conditions.append(self._group_paths(GitLabGroup.full_path))
        return conditions

    def _control_types(self) -> list[str] | None:
        if self.scope.framework is None or self.scope.control_id is None:
            return None
        if self._mapped_types is None:
            mapping = effective_mapping(self.db, self.to)
            self._mapped_types = mapping.rule_types(self.scope.framework, self.scope.control_id)
        return self._mapped_types

    def _check_targets(self) -> None:
        super()._check_targets()
        if self.to > self.generated_at:
            raise PackageConflict(
                "period_not_over",
                "The period has not ended yet",
                [f"to is after the database clock ({_ts(self.generated_at)})"],
            )
        if self.run is None:
            raise PackageConflict(
                "no_evaluation_run",
                "No evaluation run had finished by the end of the period",
                ["Results are as of the period's end, so a package needs one."],
            )
        if self.request.groups:
            known = {
                path.lower()
                for path in self.db.scalars(
                    select(GitLabGroup.full_path).where(
                        GitLabGroup.instance_id == (self.instance_id or 0),
                        func.lower(GitLabGroup.full_path).in_(
                            [group.lower() for group in self.request.groups]
                        ),
                    )
                )
            }
            missing = [group for group in self.request.groups if group.lower() not in known]
            if missing:
                raise ScopeError(f"Groups not in the inventory: {', '.join(missing[:10])}")

    def _evaluation_conditions(self) -> list[ColumnElement[bool]]:
        conditions = self._monitored_scope()
        conditions.append(Evaluation.evaluation_run_id == (self.run.id if self.run else 0))
        types = self._control_types()
        if types is not None:
            conditions.append(Evaluation.rule_type.in_(types))
        return conditions

    def _change_conditions(self) -> list[ColumnElement[bool]]:
        return [
            *self._monitored_scope(),
            ChangeRecord.changed_at >= self.request.period_from,
            ChangeRecord.changed_at < self.to,
        ]

    def plan(self) -> dict[str, int]:
        counts = super().plan()
        self.population = list(
            self.db.scalars(
                select(ChangeRecord.id)
                .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
                .where(*self._change_conditions(), ChangeRecord.kind.in_(sampling.SAMPLED_KINDS))
            )
        )
        self.sampled = sampling.draw(self.population, self.request.sample.size, self.seed)
        counts["changes"] = self._count(
            select(ChangeRecord.id)
            .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
            .where(*self._change_conditions())
            .subquery(),
            [],
        )
        counts["change_sample"] = len(self.sampled)
        return counts

    # Rows -----------------------------------------------------------------------------------

    def findings(self) -> Iterator[dict[str, Any]]:
        """Findings as they stood at the period's end."""
        if self.run is None:
            return
        statement = (
            select(Finding, GitLabProject)
            .join(GitLabProject, GitLabProject.id == Finding.project_id)
            .where(*self._monitored_scope(), Finding.first_seen_at <= self.to)
        )
        types = self._control_types()
        if types is not None:
            typed = aliased(Evaluation)
            statement = statement.where(
                select(typed.id)
                .where(typed.id == Finding.latest_evaluation_id, typed.rule_type.in_(types))
                .exists()
            )
        last = 0
        while True:
            chunk = list(
                self.db.execute(
                    statement.where(Finding.id > last).order_by(Finding.id).limit(PAGE)
                ).all()
            )
            states = findings_at(self.db, chunk, self.to, self.run, self.coverage)
            for finding, project in chunk:
                at = states.get(finding.id)
                if at is None:
                    continue
                if at.latest.observation_id is not None:
                    self._observation_ids.add(at.latest.observation_id)
                self._observation_ids.update(at.latest.observation_ids or ())
                self._policy_ids.add(at.latest.policy_id)
                if at.exception is not None:
                    self._exception_ids.add(at.exception.id)
                yield _safe(
                    {
                        "id": finding.id,
                        "project_gitlab_id": project.gitlab_id,
                        "project_path": project.path_with_namespace,
                        "policy_name": finding.policy_name,
                        "policy_id": at.latest.policy_id,
                        "rule_id": finding.rule_id,
                        "severity": finding.severity,
                        "state": at.state,
                        "resolution": at.resolution,
                        "latest_outcome": at.latest.outcome,
                        "latest_evaluation_id": at.latest.id,
                        "latest_observation_id": at.latest.observation_id,
                        "first_seen_at": _ts(finding.first_seen_at),
                        "last_seen_at": _ts(min(finding.last_seen_at, at.latest.evaluated_at)),
                        "resolved_at": _ts(finding.resolved_at)
                        if at.state == "resolved"
                        and finding.resolved_at is not None
                        and finding.resolved_at <= self.to
                        else None,
                        "exception_id": at.exception.id if at.exception else None,
                    }
                )
            if len(chunk) < PAGE:
                return
            last = chunk[-1][0].id

    def changes(self) -> Iterator[dict[str, Any]]:
        sampled = set(self.sampled)
        statement = (
            select(
                ChangeRecord,
                GitLabProject.gitlab_id,
                GitLabProject.path_with_namespace,
                Observation.payload,
                Observation.payload_digest,
                ChangeRecord.id,
            )
            .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
            .join(Observation, Observation.id == ChangeRecord.observation_id)
            .where(*self._change_conditions())
        )
        for record, gitlab_id, path, payload, digest, _ in self._pages(statement, ChangeRecord.id):
            yield _safe(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "project_gitlab_id": gitlab_id,
                    "project_path": path,
                    "iid": record.iid,
                    "deployment_id": record.deployment_id,
                    "sha": record.sha,
                    "title": (payload or {}).get("title"),
                    "changed_at": _ts(record.changed_at),
                    "outcome": record.outcome,
                    "reasons": list(record.reasons),
                    "complete": record.complete,
                    "observation_id": record.observation_id,
                    "payload_digest": digest,
                    "sampled": record.id in sampled,
                }
            )

    def change_sample(self) -> Iterator[dict[str, Any]]:
        rows = {
            record.id: (record, path)
            for record, path in self.db.execute(
                select(ChangeRecord, GitLabProject.path_with_namespace)
                .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
                .where(ChangeRecord.id.in_(self.sampled))
            ).all()
        }
        for position, change_id in enumerate(self.sampled, start=1):
            record, path = rows[change_id]
            yield _safe(
                {
                    "position": position,
                    "change_id": change_id,
                    "kind": record.kind,
                    "project_path": path,
                    "iid": record.iid,
                    "sha": record.sha,
                    "outcome": record.outcome,
                }
            )

    def change_evidence(self) -> Iterator[dict[str, Any]]:
        """Every observation of each sampled change, oldest first."""
        for start in range(0, len(self.sampled), PAGE):
            records = self.db.execute(
                select(ChangeRecord, GitLabProject)
                .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
                .where(ChangeRecord.id.in_(self.sampled[start : start + PAGE]))
                .order_by(ChangeRecord.id)
            ).all()
            for record, project in records:
                for observation in reversed(history(self.db, record, project.gitlab_id)):
                    payload = observation.payload
                    withheld = payload is not None and redact_value(payload) != payload
                    row = _safe(
                        {
                            "change_id": record.id,
                            "id": observation.id,
                            "project_gitlab_id": project.gitlab_id,
                            "project_path": project.path_with_namespace,
                            "source": observation.source,
                            "source_ref": observation.source_ref,
                            "collected_at": _ts(observation.collected_at),
                            "collection_status": observation.collection_status,
                            "collection_run_id": observation.collection_run_id,
                            "collection_job_id": observation.collection_job_id,
                            "payload_digest": observation.payload_digest,
                            "digest_valid": verify(observation),
                            "payload_withheld": withheld,
                            "error": observation.error,
                        }
                    )
                    row["payload"] = None if withheld else payload
                    yield row

    def sections(self) -> list[tuple[str, Callable[[], Iterator[dict[str, Any]]]]]:
        found = super().sections()
        at = [name for name, _ in found].index("observations")
        return [
            *found[:at],
            ("changes", self.changes),
            ("change_sample", self.change_sample),
            ("change_evidence", self.change_evidence),
            *found[at:],
        ]

    def columns(self, name: str) -> tuple[str, ...]:
        return PACKAGE_COLUMNS.get(name) or COLUMNS[name]

    # Manifest -------------------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        meta = super().metadata()
        meta["scope"] = self.request.described()
        meta["period"] = {
            "from": _ts(self.request.period_from),
            "to": _ts(self.to),
        }
        meta["sample"] = {"size": len(self.sampled), "seed": self.seed}
        return meta

    def readme(self, meta: dict[str, Any]) -> str:
        return README.format(
            export_id=self.export_id,
            generated_at=meta["generated_at"],
            instance=meta["instance"],
            format=FORMAT,
            period_from=meta["period"]["from"],
            period_to=meta["period"]["to"],
            run_id=self.run.id if self.run else None,
            run_finished=_ts(self.run.finished_at) if self.run else None,
            scope=_dumps(meta["scope"]),
            sample_size=len(self.sampled),
            population=len(self.population),
            seed=self.seed,
            population_statement=textwrap.fill(manifest.population(meta)["statement"], width=92),
        )

    def _control_mapping(self) -> dict[str, Any]:
        return mapping_summary(self.db, self.to)

    def _monitoring_scope(self) -> dict[str, Any]:
        """The exclusions in force at the period's end."""
        rows = self.db.scalars(
            select(ScopeExclusion)
            .where(
                ScopeExclusion.instance_id == (self.instance_id or 0),
                ScopeExclusion.state.in_(("active", "ended")),
                ScopeExclusion.decided_at <= self.to,
                or_(ScopeExclusion.ended_at.is_(None), ScopeExclusion.ended_at > self.to),
            )
            .order_by(ScopeExclusion.id)
        ).all()
        excluded = self._count(
            GitLabProject, [*self._project_scope(), self.coverage.project_excluded()]
        )
        return {
            "as_of": _ts(self.to),
            "exclusions": [
                {
                    "id": row.id,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "target_path": row.target_path,
                    "since": _ts(row.decided_at),
                    "justification_sha256": justification_sha256(row.justification),
                }
                for row in rows
            ],
            "excluded_projects": excluded,
        }

    def _history_gaps(self) -> dict[str, Any]:
        """Monitored projects whose change history starts after the period does."""
        rows = self.db.execute(
            select(GitLabProject.path_with_namespace, ChangeCursor.history_since)
            .join(ChangeCursor, ChangeCursor.project_id == GitLabProject.id)
            .where(*self._monitored_scope(), ChangeCursor.history_since > self.request.period_from)
            .order_by(GitLabProject.path_with_namespace)
        ).all()
        uncollected = self._count(
            GitLabProject,
            [
                *self._monitored_scope(),
                ~select(ChangeCursor.project_id)
                .where(ChangeCursor.project_id == GitLabProject.id)
                .exists(),
            ],
        )
        return {
            "projects_starting_later": len(rows),
            "projects": [
                {"project_path": path, "history_since": _ts(since)}
                for path, since in rows[:MAX_LISTED]
            ],
            "projects_truncated": len(rows) > MAX_LISTED,
            "projects_never_collected": uncollected,
        }

    def _package(self) -> dict[str, Any] | None:
        return {
            "format": FORMAT,
            "period": {"from": _ts(self.request.period_from), "to": _ts(self.to)},
            "evaluation_run": {
                "id": self.run.id if self.run else None,
                "finished_at": _ts(self.run.finished_at) if self.run else None,
            },
            "scope": self.request.described(),
            "sample": {
                "requested_size": self.request.sample.size,
                "size": len(self.sampled),
                "seed": self.seed,
                "seed_chosen_by": "requester"
                if self.request.sample.seed is not None
                else "baluarte",
                "algorithm": sampling.ALGORITHM,
                "kinds": list(sampling.SAMPLED_KINDS),
                "population": len(self.population),
                "change_ids": self.sampled,
            },
            "change_history": self._history_gaps(),
            "limitations": LIMITATIONS,
        }
