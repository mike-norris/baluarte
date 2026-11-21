"""Register export packages (roadmap M12.4, docs/ISSUE_REGISTER.md#exports).

A package is one ZIP for one of four scopes: one issue, a selection of issues, every issue mapped
to a control, or an audit period across selected controls (any scope may name a period, which
dates each cited item against it). It holds:

- ``README.txt``: a readable summary (scope, counts, gaps, limitations, how to verify)
- ``issues.csv``: each issue, why it is auditable, its category and remediation status
- ``issue_controls.csv``: the issue-to-control index, with each framework's edition and the
  catalogue version, every mapping's state and who proposed and decided it
- ``mapping_evidence.csv``: every item a mapping cites, its rationale and, with a period, whether
  it falls inside it or is later remediation
- ``decisions.csv``: every inclusion and exclusion decision on the issues
- ``observations.jsonl``: each evidence capture once, however many mappings cite it, with its
  ``payload_digest``
- ``files/<sha256>`` and ``files.csv``: captured attachments, each stored once by its SHA-256
- ``gaps.csv`` and ``redactions.csv``: what is missing or open, and what was redacted or withheld
- ``manifest.json`` and ``SHA256SUMS``, as in evidence exports (``baluarte.evidence.manifest``)

The manifest's SHA-256 is recorded in the ``register.export_completed`` audit event, which is
how a package is tied back to this deployment; ``baluarte-verify-export`` checks it offline.
Text values pass through the log redactor; a capture that would need redacting is withheld with
its digest rather than altered, as evidence exports do.
"""

import csv
import hashlib
import io
import json
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import IO, Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from baluarte import __version__
from baluarte.controls.catalogue import EDITIONS, MAPPING_VERSION, control
from baluarte.db.models import (
    AppUser,
    AuditPeriod,
    CapturedFile,
    GitLabProject,
    IssueControlMapping,
    IssueEvidence,
    Observation,
    RegisterDecision,
    RegisterIssue,
)
from baluarte.evidence.observations import verify as digest_valid
from baluarte.logs import redact_value
from baluarte.register import selection
from baluarte.register.mapping import (
    GAP_MESSAGES,
    OPEN_STATES,
    MappingView,
    Period,
    assess,
    auditable,
)
from baluarte.workspaces import current_workspace_id

EXPORT_FORMAT = "baluarte.register-export/v1"
MANIFEST_FORMAT = "baluarte.register-manifest/v1"
MAX_ISSUES = 500
MAX_CONTROLS = 100
# Captured files are copied into the package; beyond this the scope must be narrowed.
MAX_FILE_BYTES = 1024 * 1024 * 1024
CHUNK = 1024 * 1024

ISSUE_GAPS = {
    "not_captured": "The issue's evidence has not been captured.",
    "capture_partial": "Some of the issue's evidence could not be captured.",
    "capture_failed": "GitLab no longer serves the issue.",
    "not_auditable": "The issue is no longer in the register, or it is excluded.",
}
CONTROL_GAPS = {
    "no_active_mapping": "No auditable issue is mapped to the control and in force.",
    "nothing_in_period": "No issue in force cites evidence dated inside the audit period.",
}
FILE_GAPS = {"file_missing": "A cited attachment was not stored, so it is not in the package."}

DIGESTS = {
    "algorithm": "SHA-256",
    "canonical_json": (
        "JSON with keys sorted, no insignificant whitespace, non-ASCII characters unescaped, "
        "encoded as UTF-8"
    ),
    "files": "SHA-256 of the file's bytes as stored in the ZIP",
    "captured_files": "files/<sha256> is named by the SHA-256 of its bytes",
    "observations": "payload_digest is the SHA-256 of the payload's canonical JSON",
    "manifest": (
        "manifest_sha256 is the SHA-256 of this manifest's canonical JSON; it is recorded in the "
        "register.export_completed audit event, not in the package"
    ),
}

LIMITATIONS = [
    "A mapping is the organization's judgement that an issue supports a control, reviewed as "
    "recorded here; it is not a SOC 2 or ISO 27001 certification or a NIST authorization.",
    "Closing an issue is never proof that a control works: a mapping that cites only the "
    "closure is listed as a gap.",
    "Evidence is as captured from GitLab at the time recorded on each observation; anything "
    "changed in GitLab since is not reflected until it is captured again (gap recaptured).",
    "Digests show whether a row changed after it was written. They are not anchored outside the "
    "database: a database superuser could rewrite a capture and its digest together.",
    "The manifest proves the package matches what Baluarte produced only when its "
    "manifest_sha256 is compared with the register.export_completed audit event.",
    "Issues in projects the connection's token cannot read are not in the register.",
]

VERIFICATION = [
    "Run baluarte-verify-export <file> --manifest-sha256 <value from the "
    "register.export_completed audit event>.",
    "Without the tool: run sha256sum -c SHA256SUMS in the extracted directory, compare each "
    "file's hash with the manifest, check each files/<sha256> against its name, and recompute "
    "each observation's payload_digest from its payload.",
]


class ExportRefused(Exception):
    """The package cannot be built for this scope; ``code`` says why."""

    def __init__(self, code: str, message: str, reasons: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reasons = list(reasons)


@dataclass(frozen=True)
class Scope:
    issues: tuple[tuple[int, int], ...] = ()
    controls: tuple[tuple[str, str], ...] = ()
    period_id: int | None = None

    @property
    def kind(self) -> str:
        if self.issues:
            return "issue" if len(self.issues) == 1 else "issues"
        return "control" if len(self.controls) == 1 else "controls"

    def described(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "issues": [{"project_id": p, "iid": i} for p, i in self.issues],
            "controls": [{"framework": f, "control_id": c} for f, c in self.controls],
            "period_id": self.period_id,
        }


@dataclass
class Built:
    """A finished package in a temporary file, with what the audit log records about it."""

    file: IO[bytes]
    filename: str
    size: int
    sha256: str
    manifest_sha256: str
    counts: dict[str, int]
    files: dict[str, dict[str, Any]] = field(default_factory=dict)

    def chunks(self) -> Iterable[bytes]:
        try:
            self.file.seek(0)
            while chunk := self.file.read(CHUNK):
                yield chunk
        finally:
            self.file.close()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _csv(columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if row.get(k) is None else row[k] for k in columns})
    return buffer.getvalue().encode()


def _jsonl(rows: Iterable[Any]) -> bytes:
    return b"".join(canonical(row) + b"\n" for row in rows)


class _Redactions:
    """Redacts text values in rows, recording each field it changed."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def apply(self, file: str, ref: str, row: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = redact_value(row)
        for key, value in row.items():
            if cleaned[key] != value:
                self.rows.append({"file": file, "ref": ref, "field": key, "action": "redacted"})
        return cleaned

    def withheld(self, ref: str) -> None:
        self.rows.append(
            {"file": "observations.jsonl", "ref": ref, "field": "payload", "action": "withheld"}
        )


def _controls(scope: Scope) -> list[tuple[str, str]]:
    unknown = [f"{f} {c}" for f, c in scope.controls if control(f, c) is None]
    if unknown:
        raise ExportRefused("control_unknown", "A control is not in the catalogue.", unknown)
    return list(dict.fromkeys(scope.controls))


def _period(db: Session, period_id: int | None) -> AuditPeriod | None:
    if period_id is None:
        return None
    row = db.get(AuditPeriod, period_id)
    if row is None:
        raise ExportRefused("period_unknown", "No such audit period.")
    return row


def _issue_rows(db: Session, pairs: Sequence[tuple[int, int]]) -> list[Any]:
    """Each issue with its project path, category, exclusion and whether it is auditable."""
    if not pairs:
        return []
    workspace_id = current_workspace_id(db)
    rules = selection.active_rules(db, workspace_id)
    return list(
        db.execute(
            select(
                RegisterIssue,
                GitLabProject.path_with_namespace,
                selection.category(workspace_id, rules).label("category"),
                selection.excluded(workspace_id).label("excluded"),
                auditable(db).label("auditable"),
            )
            .join(GitLabProject, GitLabProject.id == RegisterIssue.project_id)
            .where(tuple_(RegisterIssue.project_id, RegisterIssue.issue_iid).in_(list(pairs)))
            .order_by(GitLabProject.path_with_namespace, RegisterIssue.issue_iid)
        ).all()
    )


def _mappings(
    db: Session, scope: Scope, controls: Sequence[tuple[str, str]]
) -> tuple[list[tuple[int, int]], list[IssueControlMapping]]:
    """The issues in scope and every mapping of them (to the scope's controls, if it names any)."""
    if scope.issues:
        pairs = list(dict.fromkeys(scope.issues))
        found = {(row[0].project_id, row[0].issue_iid): row for row in _issue_rows(db, pairs)}
        refused = [f"{p}#{i}" for p, i in pairs if (p, i) not in found or not found[(p, i)][4]]
        if refused:
            raise ExportRefused(
                "issue_not_auditable",
                "An issue is not in the register, or it is excluded.",
                refused,
            )
        rows = db.scalars(
            select(IssueControlMapping)
            .where(tuple_(IssueControlMapping.project_id, IssueControlMapping.issue_iid).in_(pairs))
            .order_by(IssueControlMapping.id)
        ).all()
        return pairs, list(rows)
    in_controls = tuple_(IssueControlMapping.framework, IssueControlMapping.control_id).in_(
        list(controls)
    )
    pairs = [
        (p, i)
        for p, i in db.execute(
            select(IssueControlMapping.project_id, IssueControlMapping.issue_iid)
            .where(in_controls, IssueControlMapping.state.in_(OPEN_STATES))
            .distinct()
            .order_by(IssueControlMapping.project_id, IssueControlMapping.issue_iid)
        )
    ]
    if len(pairs) > MAX_ISSUES:
        raise ExportRefused(
            "too_many_issues",
            f"The controls have {len(pairs)} issues mapped; a package holds at most "
            f"{MAX_ISSUES}. Choose fewer controls.",
        )
    if not pairs:
        return [], []
    rows = db.scalars(
        select(IssueControlMapping)
        .where(
            in_controls,
            tuple_(IssueControlMapping.project_id, IssueControlMapping.issue_iid).in_(pairs),
        )
        .order_by(IssueControlMapping.id)
    ).all()
    return pairs, list(rows)


def _attachments(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [
        a
        for a in (payload or {}).get("attachments") or []
        if isinstance(a, dict) and isinstance(a.get("sha256"), str)
    ]


class Builder:
    """Reads everything a package holds within the request's workspace, then writes it."""

    def __init__(
        self, db: Session, scope: Scope, export_id: str, requested_by: str, now: datetime
    ) -> None:
        self.db = db
        self.scope = scope
        self.export_id = export_id
        self.requested_by = requested_by
        self.now = now
        self.redactions = _Redactions()
        self.gaps: list[dict[str, Any]] = []

    def _gap(self, subject: str, ref: str, code: str, message: str) -> None:
        self.gaps.append({"subject": subject, "ref": ref, "code": code, "message": message})

    def build(self) -> Built:
        db = self.db
        controls = _controls(self.scope)
        period_row = _period(db, self.scope.period_id)
        period = Period(period_row.starts_on, period_row.ends_on) if period_row else None
        pairs, mappings = _mappings(db, self.scope, controls)
        views = {view.mapping.id: view for view in assess(db, mappings, period)}
        issue_rows = _issue_rows(db, pairs)
        captures = {
            (row.project_id, row.issue_iid): row
            for row in (
                db.scalars(
                    select(IssueEvidence).where(
                        tuple_(IssueEvidence.project_id, IssueEvidence.issue_iid).in_(pairs)
                    )
                )
                if pairs
                else []
            )
        }
        observation_ids = {c.observation_id for c in captures.values()} | {
            m.observation_id for m in mappings
        }
        observations = (
            db.scalars(
                select(Observation)
                .where(Observation.id.in_(observation_ids))
                .order_by(Observation.id)
            ).all()
            if observation_ids
            else []
        )
        wanted = sorted({a["sha256"] for o in observations for a in _attachments(o.payload)})
        sizes = dict(
            db.execute(
                select(CapturedFile.sha256, CapturedFile.size_bytes).where(
                    CapturedFile.sha256.in_(wanted)
                )
            ).all()
        )
        total = sum(sizes.values())
        if total > MAX_FILE_BYTES:
            raise ExportRefused(
                "too_large",
                f"The captured files come to {total} bytes; a package holds at most "
                f"{MAX_FILE_BYTES}. Narrow the scope.",
            )

        files: dict[str, bytes] = {}
        files["issues.csv"] = self._issues(issue_rows, captures)
        files["issue_controls.csv"] = self._index(mappings, views, period is not None)
        files["mapping_evidence.csv"] = self._cited(views)
        files["decisions.csv"] = self._decisions(pairs)
        captured, digest_invalid, withheld = self._observations(observations)
        files["observations.jsonl"] = captured
        names = {
            a["sha256"]: a.get("filename") for o in observations for a in _attachments(o.payload)
        }
        stored = self._files(wanted, sizes, names)
        self._control_gaps(controls, views, period is not None)
        files["files.csv"] = _csv(
            ("sha256", "size_bytes", "content_type", "filename"),
            [row for row, _ in stored.values()],
        )
        for sha, (_, content) in sorted(stored.items()):
            files[f"files/{sha}"] = content
        files["gaps.csv"] = _csv(("subject", "ref", "code", "message"), self.gaps)
        files["redactions.csv"] = _csv(("file", "ref", "field", "action"), self.redactions.rows)
        if period_row is not None:
            files["period.csv"] = _csv(
                ("id", "name", "starts_on", "ends_on", "created_by", "created_at", "ended_at"),
                [
                    {
                        "id": period_row.id,
                        "name": period_row.name,
                        "starts_on": _iso(period_row.starts_on),
                        "ends_on": _iso(period_row.ends_on),
                        "created_by": period_row.created_by,
                        "created_at": _iso(period_row.created_at),
                        "ended_at": _iso(period_row.ended_at),
                    }
                ],
            )

        states = Counter(m.state for m in mappings)
        categories = Counter(str(row[2]) for row in issue_rows)
        gap_counts = Counter(g["code"] for g in self.gaps)
        counts = {
            "issues": len(issue_rows),
            "mappings": len(mappings),
            "cited_items": sum(len(v.evidence) for v in views.values()),
            "observations": len(observations),
            "files": len(stored),
            "gaps": len(self.gaps),
            "redactions": len(self.redactions.rows),
        }
        summary = {
            "kind": self.scope.kind,
            "workspace_id": current_workspace_id(db),
            "period": None
            if period_row is None
            else {
                "id": period_row.id,
                "name": period_row.name,
                "starts_on": _iso(period_row.starts_on),
                "ends_on": _iso(period_row.ends_on),
            },
            "controls": [
                {
                    "framework": f,
                    "control_id": c,
                    "framework_edition": EDITIONS[f],
                    "title": found.title if (found := control(f, c)) else None,
                }
                for f, c in controls
            ],
            "catalogue_version": MAPPING_VERSION,
            "mappings_by_state": dict(sorted(states.items())),
            "issues_by_category": dict(sorted(categories.items())),
            "gaps_by_code": dict(sorted(gap_counts.items())),
            "files_bytes": sum(len(content) for _, content in stored.values()),
        }
        readme = self._readme(summary, counts, issue_rows, mappings)
        return self._write(
            {"README.txt": readme, **files},
            counts,
            summary,
            {
                "count": len(observations),
                "digest_invalid": len(digest_invalid),
                "digest_invalid_ids": digest_invalid,
                "payload_withheld": withheld,
            },
        )

    # Sections ----------------------------------------------------------------------------------

    def _issues(
        self, rows: Sequence[Any], captures: Mapping[tuple[int, int], IssueEvidence]
    ) -> bytes:
        db = self.db
        workspace_id = current_workspace_id(db)
        rules = selection.active_rules(db, workspace_id)
        keys = [(r[0].project_id, r[0].issue_iid) for r in rows]
        decisions = selection.open_decisions(db, workspace_id, keys)
        out = []
        for issue, path, category, is_excluded, is_auditable in rows:
            key = (issue.project_id, issue.issue_iid)
            ref = f"{issue.project_id}#{issue.issue_iid}"
            why = selection.reasons(issue, rules, decisions.get(key))
            capture = captures.get(key)
            if capture is None:
                self._gap("issue", ref, "not_captured", ISSUE_GAPS["not_captured"])
            elif capture.status == "failed":
                self._gap("issue", ref, "capture_failed", ISSUE_GAPS["capture_failed"])
            elif capture.status == "partial":
                for missing in capture.gaps:
                    self._gap(
                        "issue",
                        ref,
                        "capture_partial",
                        f"{ISSUE_GAPS['capture_partial']} {missing}",
                    )
            if not is_auditable:
                self._gap("issue", ref, "not_auditable", ISSUE_GAPS["not_auditable"])
            row = {
                "project_id": issue.project_id,
                "project": path,
                "iid": issue.issue_iid,
                "gitlab_id": issue.gitlab_id,
                "issue_type": issue.issue_type,
                "title": issue.title,
                "category": category,
                "auditable": "true" if is_auditable else "false",
                "excluded": "true" if is_excluded else "false",
                "why": "; ".join(
                    f"rule: {r.rule_name}" if r.kind == "rule" else f"{r.kind}: {r.reason}"
                    for r in why
                ),
                "state": issue.state,
                # Remediation status: open or closed in GitLab, which never by itself shows that a
                # control works.
                "remediation_status": "closed" if issue.state == "closed" else "open",
                "labels": ";".join(issue.labels),
                "author": issue.author,
                "assignees": ";".join(issue.assignees),
                "milestone": issue.milestone,
                "confidential": "true" if issue.confidential else "false",
                "opened_at": _iso(issue.opened_at),
                "updated_at": _iso(issue.source_updated_at),
                "closed_at": _iso(issue.closed_at),
                "web_url": issue.web_url,
                "read_at": _iso(issue.read_at),
                "evidence_status": capture.status if capture else None,
                "evidence_gaps": ";".join(capture.gaps) if capture else None,
                "evidence_observation_id": capture.observation_id if capture else None,
                "evidence_captured_at": _iso(capture.captured_at) if capture else None,
            }
            out.append(self.redactions.apply("issues.csv", ref, row))
        return _csv(ISSUE_COLUMNS, out)

    def _names(self, mappings: Sequence[IssueControlMapping]) -> dict[int, str | None]:
        ids = {m.proposed_by_user_id for m in mappings} | {
            m.decided_by_user_id for m in mappings if m.decided_by_user_id is not None
        }
        if not ids:
            return {}
        return dict(
            self.db.execute(
                select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))
            ).all()
        )

    def _index(
        self,
        mappings: Sequence[IssueControlMapping],
        views: Mapping[int, MappingView],
        with_period: bool,
    ) -> bytes:
        names = self._names(mappings)
        out = []
        for m in mappings:
            view = views[m.id]
            found = control(m.framework, m.control_id)
            in_force = m.state in OPEN_STATES
            if in_force:
                for gap in view.gaps:
                    self._gap("mapping", str(m.id), gap, GAP_MESSAGES[gap])
            row = {
                "mapping_id": m.id,
                "project_id": m.project_id,
                "iid": m.issue_iid,
                "framework": m.framework,
                "framework_edition": EDITIONS.get(m.framework),
                "control_id": m.control_id,
                "control_title": found.title if found else None,
                "catalogue_version": m.mapping_version,
                "state": m.state,
                "rationale": m.rationale,
                "observation_id": m.observation_id,
                "supersedes_id": m.supersedes_id,
                "review_required": "true" if m.review_required else "false",
                "self_approved": "true" if m.self_approved else "false",
                "development_decision": "true" if m.development_decision else "false",
                "proposed_by": m.proposed_by,
                "proposed_by_name": names.get(m.proposed_by_user_id),
                "proposed_at": _iso(m.proposed_at),
                "decided_by": m.decided_by,
                "decided_by_name": names.get(m.decided_by_user_id)
                if m.decided_by_user_id
                else None,
                "decided_at": _iso(m.decided_at),
                "decision_note": m.decision_note,
                "withdrawn_by": m.withdrawn_by,
                "withdrawn_at": _iso(m.withdrawn_at),
                "withdraw_reason": m.withdraw_reason,
                "cited_in_period": sum(1 for t in view.timings.values() if t == "in_period")
                if with_period
                else None,
                "gaps": ";".join(view.gaps) if in_force else None,
            }
            out.append(self.redactions.apply("issue_controls.csv", str(m.id), row))
        return _csv(INDEX_COLUMNS, out)

    def _cited(self, views: Mapping[int, MappingView]) -> bytes:
        out = []
        for mapping_id in sorted(views):
            view = views[mapping_id]
            for item in view.evidence:
                row = {
                    "mapping_id": mapping_id,
                    "position": item.position,
                    "item_key": item.item_key,
                    "kind": item.item_kind,
                    "label": item.item_label,
                    "occurred_at": _iso(item.occurred_at),
                    "timing": view.timings.get(item.item_key),
                    "rationale": item.rationale,
                }
                ref = f"{mapping_id}:{item.position}"
                out.append(self.redactions.apply("mapping_evidence.csv", ref, row))
        return _csv(CITED_COLUMNS, out)

    def _decisions(self, pairs: Sequence[tuple[int, int]]) -> bytes:
        rows = (
            self.db.scalars(
                select(RegisterDecision)
                .where(
                    RegisterDecision.workspace_id == current_workspace_id(self.db),
                    tuple_(RegisterDecision.project_id, RegisterDecision.issue_iid).in_(
                        list(pairs)
                    ),
                )
                .order_by(RegisterDecision.id)
            ).all()
            if pairs
            else []
        )
        out = [
            self.redactions.apply(
                "decisions.csv",
                str(row.id),
                {
                    "id": row.id,
                    "project_id": row.project_id,
                    "iid": row.issue_iid,
                    "decision": row.decision,
                    "category": row.category,
                    "reason": row.reason,
                    "decided_by": row.decided_by,
                    "decided_at": _iso(row.decided_at),
                    "ended_by": row.ended_by,
                    "ended_at": _iso(row.ended_at),
                },
            )
            for row in rows
        ]
        return _csv(DECISION_COLUMNS, out)

    def _observations(self, observations: Sequence[Observation]) -> tuple[bytes, list[int], int]:
        invalid: list[int] = []
        withheld = 0
        lines = []
        for o in observations:
            valid = digest_valid(o)
            if not valid:
                invalid.append(o.id)
            hide = o.payload is not None and redact_value(o.payload) != o.payload
            if hide:
                withheld += 1
                self.redactions.withheld(str(o.id))
            lines.append(
                {
                    "id": o.id,
                    "source": o.source,
                    "source_ref": o.source_ref,
                    "project_id": o.project_id,
                    "collected_at": _iso(o.collected_at),
                    "collection_status": o.collection_status,
                    "payload": None if hide else o.payload,
                    "payload_digest": o.payload_digest,
                    "digest_valid": valid,
                    "payload_withheld": hide,
                }
            )
        return _jsonl(lines), invalid, withheld

    def _files(
        self, wanted: Sequence[str], sizes: Mapping[str, int], names: Mapping[str, Any]
    ) -> dict[str, tuple[dict[str, Any], bytes]]:
        stored: dict[str, tuple[dict[str, Any], bytes]] = {}
        for sha in wanted:
            if sha not in sizes:
                self._gap("file", sha, "file_missing", FILE_GAPS["file_missing"])
                continue
            row = self.db.get_one(CapturedFile, sha)
            meta = self.redactions.apply(
                "files.csv",
                sha,
                {
                    "sha256": sha,
                    "size_bytes": row.size_bytes,
                    "content_type": row.content_type,
                    "filename": names.get(sha),
                },
            )
            stored[sha] = (meta, bytes(row.content))
        return stored

    def _control_gaps(
        self,
        controls: Sequence[tuple[str, str]],
        views: Mapping[int, MappingView],
        with_period: bool,
    ) -> None:
        for framework, control_id in controls:
            active = [
                v
                for v in views.values()
                if v.mapping.state == "active"
                and (v.mapping.framework, v.mapping.control_id) == (framework, control_id)
            ]
            ref = f"{framework} {control_id}"
            if not active:
                self._gap("control", ref, "no_active_mapping", CONTROL_GAPS["no_active_mapping"])
            elif with_period and not any("in_period" in v.timings.values() for v in active):
                self._gap("control", ref, "nothing_in_period", CONTROL_GAPS["nothing_in_period"])

    # Output ------------------------------------------------------------------------------------

    def _readme(
        self,
        summary: Mapping[str, Any],
        counts: Mapping[str, int],
        issue_rows: Sequence[Any],
        mappings: Sequence[IssueControlMapping],
    ) -> bytes:
        lines = [
            "Baluarte issue register export",
            "",
            f"Export ID:     {self.export_id}",
            f"Generated at:  {self.now.isoformat()}",
            f"Scope:         {summary['kind']}",
            f"Catalogue:     {summary['catalogue_version']}",
        ]
        if summary["period"]:
            p = summary["period"]
            lines.append(f"Audit period:  {p['name']} ({p['starts_on']} to {p['ends_on']})")
        for c in summary["controls"]:
            lines.append(f"Control:       {c['framework_edition']} {c['control_id']}: {c['title']}")
        lines += ["", "Counts"]
        lines += [f"  {name}: {value}" for name, value in counts.items()]
        if summary["issues_by_category"]:
            lines += ["", "Issues by category"]
            lines += [f"  {k}: {v}" for k, v in summary["issues_by_category"].items()]
        if summary["mappings_by_state"]:
            lines += ["", "Mappings by state"]
            lines += [f"  {k}: {v}" for k, v in summary["mappings_by_state"].items()]
        lines += ["", "Issues"]
        by_issue: dict[tuple[int, int], list[IssueControlMapping]] = {}
        for m in mappings:
            by_issue.setdefault((m.project_id, m.issue_iid), []).append(m)
        for issue, path, category, _, _ in issue_rows:
            title = redact_value(issue.title or "")
            lines.append(f"  {path}#{issue.issue_iid} [{category}, {issue.state}] {title}")
            for m in by_issue.get((issue.project_id, issue.issue_iid), []):
                lines.append(f"      {m.framework} {m.control_id}: {m.state} (mapping {m.id})")
        lines += ["", "Gaps"]
        if self.gaps:
            lines += [f"  {k}: {v}" for k, v in summary["gaps_by_code"].items()] + [
                "  Each is listed in gaps.csv."
            ]
        else:
            lines.append("  None.")
        lines += ["", "Contents", *[f"  {line}" for line in CONTENTS]]
        lines += ["", "Limitations", *[f"  - {line}" for line in LIMITATIONS]]
        lines += ["", "Verifying", *[f"  - {line}" for line in VERIFICATION], ""]
        return "\n".join(lines).encode()

    def _write(
        self,
        files: Mapping[str, bytes],
        counts: dict[str, int],
        summary: dict[str, Any],
        observations: dict[str, Any],
    ) -> Built:
        sink = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        entries: list[dict[str, Any]] = []
        sums = []
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
                digest = hashlib.sha256(data).hexdigest()
                rows = 0
                if name.endswith(".csv"):
                    rows = max(len(list(csv.reader(io.StringIO(data.decode(), newline="")))) - 1, 0)
                elif name.endswith(".jsonl"):
                    rows = sum(1 for line in data.splitlines() if line.strip())
                entries.append({"name": name, "sha256": digest, "bytes": len(data), "rows": rows})
                sums.append(f"{digest}  {name}")
            manifest = {
                "manifest_format": MANIFEST_FORMAT,
                "export_format": EXPORT_FORMAT,
                "package": "zip",
                "export_id": self.export_id,
                "generated_at": self.now.isoformat(),
                "baluarte_version": __version__,
                "scope": self.scope.described(),
                "requested_by": self.requested_by,
                "digests": DIGESTS,
                "files": entries,
                "counts": counts,
                "observations": observations,
                "register_export": summary,
                "limitations": LIMITATIONS,
                "verification": VERIFICATION,
            }
            manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
            archive.writestr("manifest.json", manifest_bytes)
            sums.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json")
            archive.writestr("SHA256SUMS", ("\n".join(sums) + "\n").encode())
        size = sink.tell()
        sink.seek(0)
        whole = hashlib.sha256()
        while chunk := sink.read(CHUNK):
            whole.update(chunk)
        stamp = self.now.strftime("%Y%m%dT%H%M%SZ")
        return Built(
            file=sink,
            filename=f"baluarte-register-{self.scope.kind}-{stamp}.zip",
            size=size,
            sha256=whole.hexdigest(),
            manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest(),
            counts=counts,
            files={e["name"]: e for e in entries if not e["name"].startswith("files/")},
        )


ISSUE_COLUMNS = (
    "project_id",
    "project",
    "iid",
    "gitlab_id",
    "issue_type",
    "title",
    "category",
    "auditable",
    "excluded",
    "why",
    "state",
    "remediation_status",
    "labels",
    "author",
    "assignees",
    "milestone",
    "confidential",
    "opened_at",
    "updated_at",
    "closed_at",
    "web_url",
    "read_at",
    "evidence_status",
    "evidence_gaps",
    "evidence_observation_id",
    "evidence_captured_at",
)
INDEX_COLUMNS = (
    "mapping_id",
    "project_id",
    "iid",
    "framework",
    "framework_edition",
    "control_id",
    "control_title",
    "catalogue_version",
    "state",
    "rationale",
    "observation_id",
    "supersedes_id",
    "review_required",
    "self_approved",
    "development_decision",
    "proposed_by",
    "proposed_by_name",
    "proposed_at",
    "decided_by",
    "decided_by_name",
    "decided_at",
    "decision_note",
    "withdrawn_by",
    "withdrawn_at",
    "withdraw_reason",
    "cited_in_period",
    "gaps",
)
CITED_COLUMNS = (
    "mapping_id",
    "position",
    "item_key",
    "kind",
    "label",
    "occurred_at",
    "timing",
    "rationale",
)
DECISION_COLUMNS = (
    "id",
    "project_id",
    "iid",
    "decision",
    "category",
    "reason",
    "decided_by",
    "decided_at",
    "ended_by",
    "ended_at",
)
CONTENTS = [
    "issues.csv            each issue, why it is auditable, category and remediation status",
    "issue_controls.csv    the issue-to-control index: framework edition, catalogue version,",
    "                      state, rationale, who proposed and decided each mapping, and gaps",
    "mapping_evidence.csv  each cited item with its rationale and timing against the period",
    "decisions.csv         inclusion and exclusion decisions on the issues",
    "observations.jsonl    each evidence capture once, with its payload_digest",
    "files.csv, files/     captured attachments, each stored once by its SHA-256",
    "gaps.csv              what is missing or still open",
    "redactions.csv        text redacted, and captures withheld, in this package",
    "period.csv            the audit period, when the scope names one",
    "manifest.json         every file's SHA-256, bytes and rows, and the summary",
    "SHA256SUMS            SHA-256 of every file, including manifest.json",
]


def build(db: Session, scope: Scope, export_id: str, requested_by: str, now: datetime) -> Built:
    return Builder(db, scope, export_id, requested_by, now).build()
