"""Scoped evidence export: one JSON document, or a ZIP of CSV files (BAL-027).

An export is read in one ``REPEATABLE READ``, read-only transaction, so every section describes
the same moment even while collection and evaluation keep running. Rows are read in pages and
written as they are read, so memory stays bounded; a scope larger than ``MAX_ROWS`` is refused
before anything is sent.

Sections, in this order (each later section only references rows of earlier ones):

- ``inventory``: projects in scope, as the inventory holds them now.
- ``evaluations``: stored rule results. Without a time window, the latest result of each
  project, policy and rule; with one, every result evaluated inside it.
- ``findings``: findings in scope, with the observation their latest evaluation judged.
- ``observations``: every observation the exported evaluations and findings reference, with
  ``digest_valid`` recomputed now.
- ``policies``: every policy version the exported evaluations and findings reference.
- ``collection_errors``: projects and groups in scope that are missing, inaccessible or carry a
  collection error. Unknown state is exported, never dropped.
- ``collection_runs``: collection and evaluation runs (all started inside the time window, or
  the latest finished run of each kind).

Nothing here exports credentials: observation payloads are already an allowlist of normalized
fields, every other text value passes through the log redactor, and a payload that would need
redacting is withheld (with its digest and ``digest_valid`` still exported) rather than altered.
"""

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PositiveInt, model_validator
from sqlalchemy import ColumnElement, Engine, and_, exists, func, or_, select, text
from sqlalchemy.orm import Session

from baluarte.db.models import (
    CollectionRun,
    Evaluation,
    EvaluationRun,
    Finding,
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    Observation,
    Policy,
)
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab import freshness as collection_freshness
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.logs import redact_value
from baluarte.policy.evaluation import freshness as evaluation_freshness

FORMAT = "baluarte.evidence-export/v1"
MAX_ROWS = 100_000
PAGE = 500

ExportFormat = Literal["json", "csv"]
Outcome = Literal["pass", "fail", "unknown", "unsupported", "excepted"]
FindingState = Literal["open", "resolved", "excepted"]

# Cells a spreadsheet would read as a formula; such cells are prefixed with an apostrophe.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# A fixed timestamp for ZIP entries, so the same evidence gives the same bytes.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

README = """Baluarte evidence export
========================

Export ID:     {export_id}
Generated at:  {generated_at} (database clock, UTC)
GitLab:        {instance}
Format:        {format}
Scope:         {scope}

Files
-----
export.json            Export metadata: scope and the collection and evaluation freshness at
                       export time. Read results together with freshness: an old pass is not
                       current compliance.
inventory.csv          Projects in scope, as the inventory holds them at export time.
evaluations.csv        Stored rule results. Without a time window, the latest result of each
                       project, policy and rule; with one, every result evaluated inside it.
findings.csv           Findings in scope and the observation their latest evaluation judged.
observations.jsonl     The evidence the exported evaluations and findings reference, one JSON
                       object per line. digest_valid recomputes the SHA-256 of the payload's
                       canonical JSON now; false means the stored payload no longer matches.
policies.csv           The exact policy versions referenced, with their YAML and its digest.
collection_errors.csv  Projects and groups in scope that are missing, inaccessible or failed to
                       collect. Unknown state is listed here, never dropped.
collection_runs.csv    Collection and evaluation runs for the window (or the latest of each).

Limitations
-----------
- unknown and unsupported results are not compliant and not failing: Baluarte could not
  establish the value, or GitLab does not offer the feature.
- A rule evaluation is supporting evidence for an organization-defined control objective; it
  is not a SOC 2 certification, ISO 27001 certification, or NIST authorization.
- Digests show whether a row changed after it was written. They are not anchored outside the
  database, so a database superuser could rewrite a row and its digest together.
- Cells that a spreadsheet would read as formulas start with an apostrophe.
"""


class ExportScope(BaseModel):
    """What to export. Every field narrows the export; ``all`` must be set to export everything."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    all: bool = Field(False, description="Export everything. Cannot be combined with filters.")
    group: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Group full path: projects in this group and all its subgroups.",
    )
    projects: list[PositiveInt] = Field(
        default_factory=list, max_length=500, description="Project GitLab IDs."
    )
    policy: str | None = Field(None, min_length=1, max_length=63, description="Policy name.")
    rule: str | None = Field(None, min_length=1, max_length=63, description="Rule ID.")
    outcome: Outcome | None = Field(None, description="Outcome of the exported evaluations.")
    finding_state: FindingState | None = Field(
        None,
        description="Only findings in this state, and evaluations of their project and rule.",
    )
    since: AwareDatetime | None = Field(None, description="Evaluated at or after (inclusive).")
    until: AwareDatetime | None = Field(None, description="Evaluated before (exclusive).")

    @model_validator(mode="after")
    def _bounded(self) -> "ExportScope":
        narrowed = [name for name in self.model_fields_set if name != "all" and getattr(self, name)]
        if self.all and narrowed:
            raise ValueError("all cannot be combined with other scope fields")
        if not self.all and not narrowed:
            raise ValueError("choose a scope, or set all to true to export everything")
        if self.since and self.until and self.since >= self.until:
            raise ValueError("since must be before until")
        if self.group is not None and self.group.strip("/") != self.group:
            raise ValueError("group is a full path without leading or trailing slashes")
        return self

    def described(self) -> dict[str, Any]:
        """The scope as stored in exports and audit events: only the fields that were set."""
        return self.model_dump(mode="json", exclude_defaults=True)


class ScopeError(ValueError):
    """The scope names something the inventory does not hold, or matches too much."""


@dataclass
class FileDigest:
    rows: int = 0
    bytes: int = 0
    sha256: str = ""


@dataclass
class ExportResult:
    """What was sent, for the completion audit event (and the BAL-028 manifest)."""

    bytes: int = 0
    sha256: str = ""
    rows: dict[str, int] = field(default_factory=dict)
    files: dict[str, FileDigest] = field(default_factory=dict)


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _like_prefix(path: str) -> str:
    escaped = path.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"


def _safe(row: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = redact_value(row)
    return redacted


class EvidenceExport:
    """One export, bound to a read-only snapshot. Call :meth:`plan`, then :meth:`stream` once."""

    def __init__(
        self,
        engine: Engine,
        settings: GitLabSettings,
        scope: ExportScope,
        fmt: ExportFormat,
        export_id: str,
    ) -> None:
        self.settings = settings
        self.scope = scope
        self.format = fmt
        self.export_id = export_id
        self.db = Session(engine)
        try:
            # The first statement of the transaction, so every later read uses one snapshot.
            self.db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            self.generated_at: datetime = self.db.execute(select(func.now())).scalar_one()
            self.instance_id: int | None = self.db.scalars(
                select(GitLabInstance.id).where(GitLabInstance.base_url == settings.url)
            ).first()
        except BaseException:
            self.db.close()
            raise
        self._observation_ids: set[int] = set()
        self._policy_ids: set[int] = set()
        self.result = ExportResult()

    def close(self) -> None:
        self.db.rollback()
        self.db.close()

    # Scope ----------------------------------------------------------------------------------

    def _project_scope(self) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [
            GitLabProject.instance_id == (self.instance_id or 0)
        ]
        if self.scope.group is not None:
            conditions.append(
                func.lower(GitLabProject.path_with_namespace).like(
                    _like_prefix(self.scope.group), escape="\\"
                )
            )
        if self.scope.projects:
            conditions.append(GitLabProject.gitlab_id.in_(self.scope.projects))
        return conditions

    def _group_scope(self) -> list[ColumnElement[bool]] | None:
        """Groups whose collection errors belong in the export; None when only projects."""
        if self.scope.projects:
            return None
        conditions: list[ColumnElement[bool]] = [GitLabGroup.instance_id == (self.instance_id or 0)]
        if self.scope.group is not None:
            path = func.lower(GitLabGroup.full_path)
            conditions.append(
                or_(
                    path == self.scope.group.lower(),
                    path.like(_like_prefix(self.scope.group), escape="\\"),
                )
            )
        return conditions

    def _check_targets(self) -> None:
        if self.scope.group is not None:
            known = self.db.scalars(
                select(GitLabGroup.id).where(
                    GitLabGroup.instance_id == (self.instance_id or 0),
                    func.lower(GitLabGroup.full_path) == self.scope.group.lower(),
                )
            ).first()
            if known is None:
                raise ScopeError(f"No group with the path {self.scope.group!r} is in the inventory")
        if self.scope.projects:
            found = set(
                self.db.scalars(
                    select(GitLabProject.gitlab_id).where(
                        GitLabProject.instance_id == (self.instance_id or 0),
                        GitLabProject.gitlab_id.in_(self.scope.projects),
                    )
                )
            )
            missing = sorted(set(self.scope.projects) - found)
            if missing:
                shown = ", ".join(str(gitlab_id) for gitlab_id in missing[:10])
                more = f" and {len(missing) - 10} more" if len(missing) > 10 else ""
                raise ScopeError(f"Projects not in the inventory: {shown}{more}")

    def _evaluation_conditions(self) -> list[ColumnElement[bool]]:
        scope = self.scope
        conditions = self._project_scope()
        if scope.policy is not None:
            conditions.append(Evaluation.policy_name == scope.policy)
        if scope.rule is not None:
            conditions.append(Evaluation.rule_id == scope.rule)
        if scope.outcome is not None:
            conditions.append(Evaluation.outcome == scope.outcome)
        if scope.since is not None:
            conditions.append(Evaluation.evaluated_at >= scope.since)
        if scope.until is not None:
            conditions.append(Evaluation.evaluated_at < scope.until)
        if scope.since is None and scope.until is None:
            latest = (
                select(func.max(Evaluation.id))
                .where(Evaluation.instance_id == (self.instance_id or 0))
                .group_by(Evaluation.project_id, Evaluation.policy_name, Evaluation.rule_id)
            )
            conditions.append(Evaluation.id.in_(latest))
        if scope.finding_state is not None:
            conditions.append(
                exists().where(
                    Finding.project_id == Evaluation.project_id,
                    Finding.policy_name == Evaluation.policy_name,
                    Finding.rule_id == Evaluation.rule_id,
                    Finding.state == scope.finding_state,
                )
            )
        return conditions

    def _finding_conditions(self) -> list[ColumnElement[bool]]:
        scope = self.scope
        conditions = self._project_scope()
        if scope.policy is not None:
            conditions.append(Finding.policy_name == scope.policy)
        if scope.rule is not None:
            conditions.append(Finding.rule_id == scope.rule)
        if scope.outcome is not None:
            conditions.append(Finding.latest_outcome == scope.outcome)
        if scope.finding_state is not None:
            conditions.append(Finding.state == scope.finding_state)
        # With a window: findings that were open at some point inside it.
        if scope.until is not None:
            conditions.append(Finding.first_seen_at < scope.until)
        if scope.since is not None:
            still_open = or_(Finding.resolved_at.is_(None), Finding.resolved_at >= scope.since)
            conditions.append(still_open)
        return conditions

    def _project_error_conditions(self) -> list[ColumnElement[bool]]:
        return [
            *self._project_scope(),
            or_(GitLabProject.state != "active", GitLabProject.last_error.is_not(None)),
        ]

    def _group_error_conditions(self) -> list[ColumnElement[bool]] | None:
        groups = self._group_scope()
        if groups is None:
            return None
        return [*groups, or_(GitLabGroup.state != "active", GitLabGroup.last_error.is_not(None))]

    def _count(self, model: Any, conditions: Sequence[ColumnElement[bool]] | None) -> int:
        if conditions is None:
            return 0
        return self.db.execute(
            select(func.count()).select_from(model).where(*conditions)
        ).scalar_one()

    def plan(self) -> dict[str, int]:
        """Check the scope and count the rows it selects; raises :class:`ScopeError`."""
        self._check_targets()
        evaluations = (
            select(Evaluation.id)
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .where(*self._evaluation_conditions())
        )
        findings = (
            select(Finding.id)
            .join(GitLabProject, GitLabProject.id == Finding.project_id)
            .where(*self._finding_conditions())
        )
        counts = {
            "inventory": self._count(GitLabProject, self._project_scope()),
            "evaluations": self._count(evaluations.subquery(), []),
            "findings": self._count(findings.subquery(), []),
            "collection_errors": self._count(GitLabProject, self._project_error_conditions())
            + self._count(GitLabGroup, self._group_error_conditions()),
        }
        total = sum(counts.values())
        if total > MAX_ROWS:
            raise ScopeError(
                f"This scope selects {total} rows; an export holds at most {MAX_ROWS}. "
                "Narrow it by group, project, policy, rule or time window."
            )
        return counts

    # Rows -----------------------------------------------------------------------------------

    def _pages(self, statement: Any, key: Any) -> Iterator[Any]:
        last: Any = None
        while True:
            page = statement if last is None else statement.where(key > last)
            rows = self.db.execute(page.order_by(key).limit(PAGE)).all()
            yield from rows
            if len(rows) < PAGE:
                return
            last = rows[-1][-1]

    def inventory(self) -> Iterator[dict[str, Any]]:
        statement = select(GitLabProject, GitLabProject.id).where(*self._project_scope())
        for project, _ in self._pages(statement, GitLabProject.id):
            yield _safe(
                {
                    "gitlab_id": project.gitlab_id,
                    "path_with_namespace": project.path_with_namespace,
                    "name": project.name,
                    "namespace_gitlab_id": project.namespace_gitlab_id,
                    "namespace_kind": project.namespace_kind,
                    "visibility": project.visibility,
                    "archived": project.archived,
                    "default_branch": project.default_branch,
                    "state": project.state,
                    "first_seen_at": _ts(project.first_seen_at),
                    "last_seen_at": _ts(project.last_seen_at),
                    "last_error": project.last_error,
                    "last_error_at": _ts(project.last_error_at),
                }
            )

    def evaluations(self) -> Iterator[dict[str, Any]]:
        statement = (
            select(
                Evaluation,
                Policy.version,
                GitLabProject.gitlab_id,
                GitLabProject.path_with_namespace,
                Evaluation.id,
            )
            .join(Policy, Policy.id == Evaluation.policy_id)
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .where(*self._evaluation_conditions())
        )
        for evaluation, version, gitlab_id, path, _ in self._pages(statement, Evaluation.id):
            if evaluation.observation_id is not None:
                self._observation_ids.add(evaluation.observation_id)
            self._policy_ids.add(evaluation.policy_id)
            yield _safe(
                {
                    "id": evaluation.id,
                    "evaluated_at": _ts(evaluation.evaluated_at),
                    "evaluation_run_id": evaluation.evaluation_run_id,
                    "project_gitlab_id": gitlab_id,
                    "project_path": path,
                    "policy_name": evaluation.policy_name,
                    "policy_version": version,
                    "policy_id": evaluation.policy_id,
                    "rule_id": evaluation.rule_id,
                    "rule_type": evaluation.rule_type,
                    "severity": evaluation.severity,
                    "outcome": evaluation.outcome,
                    "reason": evaluation.reason,
                    "unknown_reasons": list(evaluation.unknown_reasons),
                    "expected": evaluation.expected,
                    "observed": evaluation.observed,
                    "source": evaluation.source,
                    "observed_at": _ts(evaluation.observed_at),
                    "observation_id": evaluation.observation_id,
                    "capability_name": evaluation.capability_name,
                    "capability_status": evaluation.capability_status,
                    "collection_kind": evaluation.collection_kind,
                    "collection_status": evaluation.collection_status,
                    "collection_last_success_at": _ts(evaluation.collection_last_success_at),
                    "ancestry": evaluation.ancestry,
                }
            )

    def findings(self) -> Iterator[dict[str, Any]]:
        statement = (
            select(
                Finding,
                GitLabProject.gitlab_id,
                GitLabProject.path_with_namespace,
                Evaluation.observation_id,
                Finding.id,
            )
            .join(GitLabProject, GitLabProject.id == Finding.project_id)
            .join(Evaluation, Evaluation.id == Finding.latest_evaluation_id)
            .where(*self._finding_conditions())
        )
        for finding, gitlab_id, path, observation_id, _ in self._pages(statement, Finding.id):
            if observation_id is not None:
                self._observation_ids.add(observation_id)
            self._policy_ids.add(finding.policy_id)
            yield _safe(
                {
                    "id": finding.id,
                    "project_gitlab_id": gitlab_id,
                    "project_path": path,
                    "policy_name": finding.policy_name,
                    "policy_id": finding.policy_id,
                    "rule_id": finding.rule_id,
                    "severity": finding.severity,
                    "state": finding.state,
                    "resolution": finding.resolution,
                    "latest_outcome": finding.latest_outcome,
                    "latest_evaluation_id": finding.latest_evaluation_id,
                    "latest_observation_id": observation_id,
                    "first_seen_at": _ts(finding.first_seen_at),
                    "last_seen_at": _ts(finding.last_seen_at),
                    "resolved_at": _ts(finding.resolved_at),
                }
            )

    def observations(self) -> Iterator[dict[str, Any]]:
        ids = sorted(self._observation_ids)
        for start in range(0, len(ids), PAGE):
            rows = self.db.execute(
                select(Observation, GitLabProject.gitlab_id, GitLabProject.path_with_namespace)
                .outerjoin(GitLabProject, GitLabProject.id == Observation.project_id)
                .where(Observation.id.in_(ids[start : start + PAGE]))
                .order_by(Observation.id)
            ).all()
            for observation, gitlab_id, path in rows:
                payload = observation.payload
                withheld = payload is not None and redact_value(payload) != payload
                row = _safe(
                    {
                        "id": observation.id,
                        "project_gitlab_id": gitlab_id,
                        "project_path": path,
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
                # The payload is exported exactly as stored, so its digest can be checked.
                row["payload"] = None if withheld else payload
                yield row

    def policies(self) -> Iterator[dict[str, Any]]:
        ids = sorted(self._policy_ids)
        for start in range(0, len(ids), PAGE):
            for policy in self.db.scalars(
                select(Policy).where(Policy.id.in_(ids[start : start + PAGE])).order_by(Policy.id)
            ):
                source = redact_value(policy.source_yaml)
                yield {
                    "id": policy.id,
                    "name": policy.name,
                    "version": policy.version,
                    "yaml_digest": policy.yaml_digest,
                    "effective_at": _ts(policy.effective_at),
                    "created_at": _ts(policy.created_at),
                    "source_yaml_redacted": source != policy.source_yaml,
                    "source_yaml": source,
                }

    def collection_errors(self) -> Iterator[dict[str, Any]]:
        projects = select(GitLabProject, GitLabProject.id).where(*self._project_error_conditions())
        for project, _ in self._pages(projects, GitLabProject.id):
            yield _safe(
                {
                    "resource_type": "project",
                    "gitlab_id": project.gitlab_id,
                    "path": project.path_with_namespace,
                    "state": project.state,
                    "error": project.last_error,
                    "error_at": _ts(project.last_error_at),
                    "last_seen_at": _ts(project.last_seen_at),
                }
            )
        conditions = self._group_error_conditions()
        if conditions is None:
            return
        groups = select(GitLabGroup, GitLabGroup.id).where(*conditions)
        for group, _ in self._pages(groups, GitLabGroup.id):
            yield _safe(
                {
                    "resource_type": "group",
                    "gitlab_id": group.gitlab_id,
                    "path": group.full_path,
                    "state": group.state,
                    "error": group.last_error,
                    "error_at": _ts(group.last_error_at),
                    "last_seen_at": _ts(group.last_seen_at),
                }
            )

    def _runs(self, model: Any, kind: Any) -> list[Any]:
        instance = model.instance_id == (self.instance_id or 0)
        if self.scope.since is not None or self.scope.until is not None:
            window: list[ColumnElement[bool]] = [instance]
            if self.scope.since is not None:
                window.append(model.started_at >= self.scope.since)
            if self.scope.until is not None:
                window.append(model.started_at < self.scope.until)
            return list(self.db.scalars(select(model).where(*window).order_by(model.id)))
        finished = and_(instance, model.status != "running")
        if kind is None:
            latest = select(func.max(model.id)).where(finished)
        else:
            latest = select(func.max(model.id)).where(finished).group_by(kind)
        return list(self.db.scalars(select(model).where(model.id.in_(latest)).order_by(model.id)))

    def collection_runs(self) -> Iterator[dict[str, Any]]:
        for run in self._runs(CollectionRun, CollectionRun.kind):
            yield _safe(self._run_row("collection", run.kind, run))
        for run in self._runs(EvaluationRun, None):
            yield _safe(self._run_row("evaluation", "evaluation", run))

    @staticmethod
    def _run_row(run_type: str, kind: str, run: Any) -> dict[str, Any]:
        return {
            "run_type": run_type,
            "id": run.id,
            "kind": kind,
            "status": run.status,
            "started_at": _ts(run.started_at),
            "finished_at": _ts(run.finished_at),
            "error": run.error,
            "counts": run.counts,
        }

    def metadata(self) -> dict[str, Any]:
        collection = collection_freshness.report(self.db, self.instance_id, self.settings)
        evaluation = evaluation_freshness.report(self.db, self.instance_id, self.settings)
        return _safe(
            {
                "format": FORMAT,
                "export_id": self.export_id,
                "generated_at": _ts(self.generated_at),
                "instance": self.settings.url,
                "scope": self.scope.described(),
                "freshness": {
                    "collection": collection.model_dump(mode="json"),
                    "evaluation": evaluation.model_dump(mode="json"),
                },
            }
        )

    def sections(self) -> list[tuple[str, Callable[[], Iterator[dict[str, Any]]]]]:
        return [
            ("inventory", self.inventory),
            ("evaluations", self.evaluations),
            ("findings", self.findings),
            ("observations", self.observations),
            ("policies", self.policies),
            ("collection_errors", self.collection_errors),
            ("collection_runs", self.collection_runs),
        ]

    # Output ---------------------------------------------------------------------------------

    def filename(self) -> str:
        stamp = self.generated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        extension = "json" if self.format == "json" else "zip"
        return f"baluarte-evidence-{stamp}-{self.export_id[:8]}.{extension}"

    def stream(self, on_complete: Callable[[ExportResult], None]) -> Iterator[bytes]:
        """The export's bytes. ``on_complete`` runs only when every byte has been produced."""
        digest = hashlib.sha256()
        try:
            chunks = self._json() if self.format == "json" else self._zip()
            for chunk in chunks:
                if chunk:
                    digest.update(chunk)
                    self.result.bytes += len(chunk)
                    yield chunk
            self.result.sha256 = digest.hexdigest()
            on_complete(self.result)
        finally:
            self.close()

    def _json(self) -> Iterator[bytes]:
        meta = self.metadata()
        head = _dumps(meta)[:-1]  # leave the object open for the sections
        yield head.encode()
        for name, rows in self.sections():
            yield f',\n"{name}":['.encode()
            count = 0
            for row in rows():
                yield (("," if count else "") + "\n" + _dumps(row)).encode()
                count += 1
            yield b"\n]"
            self.result.rows[name] = count
        yield (',\n"counts":' + _dumps(self.result.rows) + "}\n").encode()

    def _zip(self) -> Iterator[bytes]:
        sink = _Sink()
        archive = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED)
        meta = self.metadata()
        readme = README.format(
            export_id=self.export_id,
            generated_at=meta["generated_at"],
            instance=meta["instance"],
            format=FORMAT,
            scope=_dumps(meta["scope"]),
        )
        yield from self._zip_file(archive, sink, "README.txt", iter([readme]), rows=False)
        meta_text = json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
        yield from self._zip_file(archive, sink, "export.json", iter([meta_text]), rows=False)
        for name, rows in self.sections():
            if name == "observations":
                lines = (_dumps(row) + "\n" for row in rows())
                yield from self._zip_file(archive, sink, "observations.jsonl", lines)
                self.result.rows[name] = self.result.files["observations.jsonl"].rows
            else:
                filename = f"{name}.csv"
                yield from self._zip_file(archive, sink, filename, _csv(COLUMNS[name], rows()))
                self.result.rows[name] = self.result.files[filename].rows
        archive.close()
        yield sink.drain()

    def _zip_file(
        self,
        archive: zipfile.ZipFile,
        sink: "_Sink",
        name: str,
        parts: Iterable[str],
        *,
        rows: bool = True,
    ) -> Iterator[bytes]:
        info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        digest = hashlib.sha256()
        record = FileDigest()
        with archive.open(info, "w") as handle:
            for index, part in enumerate(parts):
                data = part.encode()
                digest.update(data)
                record.bytes += len(data)
                if rows and not (name.endswith(".csv") and index == 0):
                    record.rows += 1
                handle.write(data)
                yield sink.drain()
        record.sha256 = digest.hexdigest()
        self.result.files[name] = record
        yield sink.drain()


COLUMNS: dict[str, tuple[str, ...]] = {
    "inventory": (
        "gitlab_id",
        "path_with_namespace",
        "name",
        "namespace_gitlab_id",
        "namespace_kind",
        "visibility",
        "archived",
        "default_branch",
        "state",
        "first_seen_at",
        "last_seen_at",
        "last_error",
        "last_error_at",
    ),
    "evaluations": (
        "id",
        "evaluated_at",
        "evaluation_run_id",
        "project_gitlab_id",
        "project_path",
        "policy_name",
        "policy_version",
        "policy_id",
        "rule_id",
        "rule_type",
        "severity",
        "outcome",
        "reason",
        "unknown_reasons",
        "expected",
        "observed",
        "source",
        "observed_at",
        "observation_id",
        "capability_name",
        "capability_status",
        "collection_kind",
        "collection_status",
        "collection_last_success_at",
        "ancestry",
    ),
    "findings": (
        "id",
        "project_gitlab_id",
        "project_path",
        "policy_name",
        "policy_id",
        "rule_id",
        "severity",
        "state",
        "resolution",
        "latest_outcome",
        "latest_evaluation_id",
        "latest_observation_id",
        "first_seen_at",
        "last_seen_at",
        "resolved_at",
    ),
    "policies": (
        "id",
        "name",
        "version",
        "yaml_digest",
        "effective_at",
        "created_at",
        "source_yaml_redacted",
        "source_yaml",
    ),
    "collection_errors": (
        "resource_type",
        "gitlab_id",
        "path",
        "state",
        "error",
        "error_at",
        "last_seen_at",
    ),
    "collection_runs": (
        "run_type",
        "id",
        "kind",
        "status",
        "started_at",
        "finished_at",
        "error",
        "counts",
    ),
}


class _Sink:
    """A write-only file for :class:`zipfile.ZipFile` that hands its bytes out as they come."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def cell(value: Any) -> str:
    """One CSV cell. Text a spreadsheet would run as a formula gets a leading apostrophe."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    rendered = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if rendered.startswith(_FORMULA_START):
        return "'" + rendered
    return rendered


def _csv(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    def line(values: Iterable[str]) -> str:
        writer.writerow(values)
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return text

    yield line(columns)
    for row in rows:
        yield line(cell(row[column]) for column in columns)
