"""Audit release packages: one production release and its evidence (RG.4).

docs/RELEASE_GOVERNANCE.md#audit-release-export-rg4. A package is one ZIP for one production
deployment, built from stored evidence plus the release pipeline's reports. It never holds source
code. It holds:

- ``README.txt``: a readable summary (the release, who did what, the judgment, gaps, how to
  verify)
- ``release.json``: the deployment, who started, approved and deployed it, its GitLab release,
  the practice in force and the release judged against it, the release protections in force, the
  previous production release and the range of changes
- ``release_notes.md``: the GitLab release's notes, when it has some
- ``merge_requests.csv``: the merge requests and direct pushes since the previous production
  release, with approvals, review, pipeline and tests
- ``observations.jsonl``: every observation the package rests on, with its ``payload_digest``
- ``reports.csv`` and ``reports/<sha256>``: the release pipeline's reports, each stored once by
  its SHA-256 or listed with why it was not fetched (``baluarte.releases.reports``)
- ``gaps.csv``: what is missing or could not be judged
- ``manifest.json`` and ``SHA256SUMS``, as in evidence exports

The manifest's SHA-256 is recorded in the ``release.export_completed`` audit event;
``baluarte-verify-export`` checks a package offline. Text values pass through the log redactor; an
observation that would need redacting is withheld with its digest rather than altered.
"""

import csv
import hashlib
import io
import json
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import __version__
from baluarte.db.models import ChangeRecord, GitLabInstance, GitLabProject, Observation
from baluarte.evidence.observations import verify as digest_valid
from baluarte.integrations.gitlab.changes import by_sha, iso, parse_time
from baluarte.integrations.gitlab.release_protections import RELEASE_PROTECTION_SOURCE
from baluarte.logs import redact_value
from baluarte.register.export import CHUNK, Built
from baluarte.releases import checks, practices
from baluarte.releases.reports import STATUS_TEXT, Fetched

EXPORT_FORMAT = "baluarte.release-export/v1"
MANIFEST_FORMAT = "baluarte.release-manifest/v1"
# Changes in one package; beyond this the oldest are left out and the gap says so.
MAX_CHANGES = 500
# Earlier deployments scanned for the previous production release of the same environment.
MAX_PREVIOUS = 200

Fetcher = Callable[[int, int], Fetched]

DIGESTS = {
    "algorithm": "SHA-256",
    "canonical_json": (
        "JSON with keys sorted, no insignificant whitespace, non-ASCII characters unescaped, "
        "encoded as UTF-8"
    ),
    "files": "SHA-256 of the file's bytes as stored in the ZIP",
    "reports": "reports/<sha256> is named by the SHA-256 of its bytes",
    "observations": "payload_digest is the SHA-256 of the payload's canonical JSON",
    "manifest": (
        "manifest_sha256 is the SHA-256 of this manifest's canonical JSON; it is recorded in the "
        "release.export_completed audit event, not in the package"
    ),
}

LIMITATIONS = [
    "The package holds what GitLab recorded and Baluarte read: people are GitLab usernames, and "
    "times are GitLab's. It never holds source code.",
    "The changes are those merged into the default branch after the change the previous "
    "production release shipped, up to the one this release shipped, by merge time. Baluarte "
    "does not read git history, so a change merged and then left out of the tagged commit would "
    "still be listed.",
    "The practice is the one in force when the package was built, not necessarily when the "
    "release was made; assignments are audited as release_practice.assigned.",
    "The release protections are the latest read at or before the release finished; when none "
    "had been read by then, the next read is used and listed as a gap.",
    "Reports are fetched from GitLab when the package is built; an artifact that has expired, is "
    "too large or is kept only as a report artifact is listed, not included.",
    "Digests show whether a row changed after it was written. They are not anchored outside the "
    "database: a database superuser could rewrite an observation and its digest together.",
    "The manifest proves the package matches what Baluarte produced only when its "
    "manifest_sha256 is compared with the release.export_completed audit event.",
]

VERIFICATION = [
    "Run baluarte-verify-export <file> --manifest-sha256 <value from the "
    "release.export_completed audit event>.",
    "Without the tool: run sha256sum -c SHA256SUMS in the extracted directory, compare each "
    "file's hash with the manifest, check each reports/<sha256> against its name, and recompute "
    "each observation's payload_digest from its payload.",
]

CONTENTS = [
    "release.json          the deployment, who started, approved and deployed it, its GitLab",
    "                      release, the practice and judgment, protections, previous release",
    "release_notes.md      the GitLab release's notes, when it has some",
    "merge_requests.csv    the changes since the previous production release, with approvals,",
    "                      review, pipeline and tests",
    "observations.jsonl    every observation the package rests on, with its payload_digest",
    "reports.csv, reports/ the release pipeline's reports, each stored once by its SHA-256",
    "gaps.csv              what is missing or could not be judged",
    "manifest.json         every file's SHA-256, bytes and rows, and the summary",
    "SHA256SUMS            SHA-256 of every file, including manifest.json",
]

CHANGE_COLUMNS = (
    "change_id",
    "kind",
    "iid",
    "sha",
    "title",
    "author",
    "merged_by",
    "merged_at",
    "approved_by",
    "self_approved",
    "review_outcome",
    "outcome",
    "pipeline_id",
    "pipeline_status",
    "tests_status",
    "tests_total",
    "tests_failed",
    "shipped",
    "web_path",
    "observation_id",
)
REPORT_COLUMNS = (
    "file_type",
    "job_id",
    "job_name",
    "filename",
    "listed_bytes",
    "status",
    "status_text",
    "sha256",
    "size_bytes",
    "expire_at",
)
GAP_COLUMNS = ("code", "ref", "message")


class ExportRefused(Exception):
    """The package cannot be built; ``code`` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _csv(columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if row.get(k) is None else row[k] for k in columns})
    return buffer.getvalue().encode()


def _jsonl(rows: Iterable[Any]) -> bytes:
    return b"".join(canonical(row) + b"\n" for row in rows)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def find_release(db: Session, change_id: int) -> tuple[ChangeRecord, GitLabProject]:
    """The production deployment ``change_id`` and its project, in this workspace."""
    found = db.execute(
        select(ChangeRecord, GitLabProject)
        .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
        .where(ChangeRecord.id == change_id)
    ).first()
    if found is None:
        raise ExportRefused("release_unknown", f"No change {change_id} in this workspace")
    record, project = found
    if record.kind != "deployment":
        raise ExportRefused(
            "not_a_release", f"Change {change_id} is a {record.kind}, not a production deployment"
        )
    return record, project


class Builder:
    def __init__(
        self,
        db: Session,
        change_id: int,
        export_id: str,
        requested_by: str,
        now: datetime,
        fetch: Fetcher | None,
    ) -> None:
        self.db = db
        self.change_id = change_id
        self.export_id = export_id
        self.requested_by = requested_by
        self.now = now
        self.fetch = fetch
        self.gaps: list[dict[str, Any]] = []
        self.observations: dict[int, Observation] = {}

    def _gap(self, code: str, ref: str, message: str) -> None:
        self.gaps.append({"code": code, "ref": ref, "message": message})

    def _use(self, observation: Observation | None) -> int | None:
        if observation is None:
            return None
        self.observations[observation.id] = observation
        return observation.id

    # Reading ------------------------------------------------------------------------------------

    def _previous(
        self, record: ChangeRecord, environment: Any
    ) -> tuple[ChangeRecord, dict[str, Any]] | None:
        rows = self.db.execute(
            select(ChangeRecord, Observation)
            .join(Observation, Observation.id == ChangeRecord.observation_id)
            .where(
                ChangeRecord.project_id == record.project_id,
                ChangeRecord.kind == "deployment",
                ChangeRecord.changed_at < record.changed_at,
            )
            .order_by(ChangeRecord.changed_at.desc(), ChangeRecord.id.desc())
            .limit(MAX_PREVIOUS)
        ).all()
        for earlier, observation in rows:
            payload = _dict(observation.payload)
            if payload.get("environment") == environment:
                return earlier, payload
        return None

    def _changes(self, record: ChangeRecord) -> list[tuple[ChangeRecord, Observation]]:
        """The project's merge requests and direct pushes up to this release, newest first."""
        return list(
            self.db.execute(
                select(ChangeRecord, Observation)
                .join(Observation, Observation.id == ChangeRecord.observation_id)
                .where(
                    ChangeRecord.project_id == record.project_id,
                    ChangeRecord.kind.in_(("merge_request", "direct_push")),
                    ChangeRecord.changed_at <= record.changed_at,
                )
                .order_by(ChangeRecord.changed_at.desc(), ChangeRecord.id.desc())
            ).all()
        )

    def _protections(self, project: GitLabProject, at: datetime) -> tuple[Observation | None, bool]:
        """The latest release protections read by ``at``, or the first read after it."""
        query = select(Observation).where(
            Observation.project_id == project.id,
            Observation.source == RELEASE_PROTECTION_SOURCE,
        )
        before = self.db.scalars(
            query.where(Observation.collected_at <= at)
            .order_by(Observation.collected_at.desc(), Observation.id.desc())
            .limit(1)
        ).first()
        if before is not None:
            return before, False
        after = self.db.scalars(
            query.order_by(Observation.collected_at, Observation.id).limit(1)
        ).first()
        return after, after is not None

    # Building -----------------------------------------------------------------------------------

    def build(self) -> Built:
        record, project = find_release(self.db, self.change_id)
        instance = self.db.get_one(GitLabInstance, project.instance_id)
        deployment = self.db.get_one(Observation, record.observation_id)
        payload = _dict(deployment.payload)
        self._use(deployment)
        name = f"deployment {record.deployment_id}"
        finished = parse_time(payload.get("finished_at")) or record.changed_at

        # The changes since the previous production release of the same environment.
        changes = self._changes(record)
        shas = by_sha(_dict(o.payload) for _, o in reversed(changes))

        def shipped(sha: Any) -> ChangeRecord | None:
            found = shas.get(sha) if isinstance(sha, str) else None
            if found is None:
                return None
            return next(
                (c for c, o in changes if _dict(o.payload) is found),
                None,
            )

        this_change = shipped(payload.get("sha"))
        previous = self._previous(record, payload.get("environment"))
        upper = this_change.changed_at if this_change is not None else record.changed_at
        basis = "shipped_commits"
        if this_change is None:
            basis = "deployment_times"
            self._gap(
                "shipped_change_unknown",
                name,
                "The change this release shipped is not recorded, so the range ends at the "
                "deployment's time.",
            )
        lower = None
        if previous is not None:
            earlier, earlier_payload = previous
            before = shipped(earlier_payload.get("sha"))
            if before is not None:
                lower = before.changed_at
            else:
                lower = earlier.changed_at
                basis = "deployment_times"
                self._gap(
                    "previous_change_unknown",
                    f"deployment {earlier.deployment_id}",
                    "The change the previous release shipped is not recorded, so the range starts "
                    "at its deployment's time.",
                )
        else:
            self._gap(
                "first_release",
                name,
                "No earlier production release of this environment is recorded, so the range "
                "starts with the project's recorded history.",
            )
        included = [
            (c, o)
            for c, o in reversed(changes)
            if c.changed_at <= upper and (lower is None or c.changed_at > lower)
        ]
        if len(included) > MAX_CHANGES:
            self._gap(
                "changes_truncated",
                name,
                f"{len(included)} changes are in the range; the {MAX_CHANGES} most recent are "
                "listed.",
            )
            included = included[-MAX_CHANGES:]
        if this_change is not None and all(c.id != this_change.id for c, _ in included):
            # A redeploy of what an earlier release shipped: nothing new, but what it shipped is
            # still listed.
            included.append(next((c, o) for c, o in changes if c.id == this_change.id))
            self._gap(
                "nothing_new",
                name,
                "The previous production release shipped the same change or a later one; nothing "
                "new was merged in between. The change this release shipped is listed.",
            )
        rows = [self._change_row(c, o, this_change) for c, o in included]

        # The practice and the release judged against it.
        resolved = practices.resolve(self.db, instance, project)
        protections, after = self._protections(project, finished)
        protection_payload = _dict(protections.payload) if protections is not None else None
        if protections is None:
            self._gap(
                "protections_missing", name, "The project's release protections were never read."
            )
        elif after:
            self._gap(
                "protections_read_after_release",
                name,
                "No release protections had been read when the release finished; the first read "
                "after it is used.",
            )
        judgment = None
        if resolved is None:
            self._gap("no_practice", name, "No release practice applies to the project.")
        else:
            judged = checks.judge(
                payload,
                resolved.settings,
                protection_payload,
                shas,
                project.default_branch,
            )
            judgment = {"verdict": judged.verdict, "reasons": judged.reasons}
        approvals_part = _dict(payload.get("deployment_approvals"))
        if approvals_part.get("status") != "ok":
            self._gap(
                "approvals_not_read",
                name,
                f"The deployment's approvals were not read ({approvals_part.get('status')}).",
            )
        release = _dict(payload.get("release"))
        if release.get("status") != "ok":
            self._gap(
                "no_gitlab_release",
                name,
                "GitLab has no release for the deployed tag, or it was not read.",
            )
        elif release.get("notes_truncated"):
            self._gap("notes_truncated", name, "The release notes were longer than Baluarte keeps.")
        if release.get("evidences"):
            self._gap(
                "release_evidence_not_fetched",
                name,
                "GitLab serves its release evidence file only through its web interface; its "
                "SHA-256 and collection time are in release.json.",
            )

        # Reports.
        reports = self._reports(project, payload)

        protections_id = self._use(protections)
        trigger = _dict(payload.get("trigger"))
        document = redact_value(
            {
                "project": {
                    "gitlab_id": project.gitlab_id,
                    "path": project.path_with_namespace,
                    "default_branch": project.default_branch,
                },
                "connection": instance.base_url,
                "deployment": {
                    "change_id": record.id,
                    "id": record.deployment_id,
                    "iid": payload.get("iid"),
                    "environment": payload.get("environment"),
                    "ref": payload.get("ref"),
                    "tag": payload.get("tag"),
                    "sha": payload.get("sha"),
                    "created_at": payload.get("created_at"),
                    "finished_at": payload.get("finished_at"),
                    "job": payload.get("job"),
                    "pipeline_id": payload.get("pipeline_id"),
                    "observation_id": deployment.id,
                },
                "roles": {
                    "started_by": trigger.get("user") if trigger.get("status") == "ok" else None,
                    "started_by_source": trigger.get("source"),
                    "approvals": approvals_part.get("approvals"),
                    "approvals_status": approvals_part.get("status"),
                    "deployer": payload.get("deployer"),
                },
                "release": {k: v for k, v in release.items() if k != "notes"} or None,
                "practice": resolved.as_dict() if resolved is not None else None,
                "judgment": judgment,
                "protections": (
                    {
                        "observation_id": protections_id,
                        "collected_at": iso(protections.collected_at),
                        "read_after_release": after,
                        **(protection_payload or {}),
                    }
                    if protections is not None
                    else None
                ),
                "previous_release": (
                    {
                        "change_id": previous[0].id,
                        "deployment_id": previous[0].deployment_id,
                        "ref": previous[1].get("ref"),
                        "sha": previous[1].get("sha"),
                        "finished_at": previous[1].get("finished_at"),
                    }
                    if previous is not None
                    else None
                ),
                "changes": {
                    "basis": basis,
                    "after": iso(lower) if lower is not None else None,
                    "through": iso(upper),
                    "count": len(rows),
                },
            }
        )
        files: dict[str, bytes] = {
            "release.json": json.dumps(document, indent=2, ensure_ascii=False).encode()
        }
        notes = release.get("notes")
        if isinstance(notes, str) and notes.strip():
            files["release_notes.md"] = redact_value(notes).encode()
        files["merge_requests.csv"] = _csv(CHANGE_COLUMNS, rows)
        withheld = 0
        observation_rows = []
        invalid = 0
        for observation in sorted(self.observations.values(), key=lambda o: o.id):
            row, was_withheld, valid = self._observation_row(observation, project)
            withheld += was_withheld
            invalid += not valid
            observation_rows.append(row)
        files["observations.jsonl"] = _jsonl(observation_rows)
        report_rows = []
        for report in reports.reports:
            report_rows.append(
                {
                    "file_type": report.file_type,
                    "job_id": report.job_id,
                    "job_name": report.job_name,
                    "filename": report.filename,
                    "listed_bytes": report.listed_bytes,
                    "status": report.status,
                    "status_text": STATUS_TEXT.get(report.status, report.status),
                    "sha256": report.sha256,
                    "size_bytes": len(report.data) if report.data is not None else None,
                    "expire_at": report.expire_at,
                }
            )
            if report.data is not None and report.sha256 is not None:
                files[f"reports/{report.sha256}"] = report.data
            elif report.status not in ("in_test_report",):
                self._gap(
                    f"report_{report.status}",
                    f"{report.file_type} {report.filename or ''} (job {report.job_id})".strip(),
                    STATUS_TEXT.get(report.status, report.status),
                )
        files["reports.csv"] = _csv(REPORT_COLUMNS, report_rows)
        files["gaps.csv"] = _csv(GAP_COLUMNS, self.gaps)
        fetched = [r for r in reports.reports if r.data is not None]
        counts = {
            "changes": len(rows),
            "observations": len(observation_rows),
            "reports_listed": len(reports.reports),
            "reports_fetched": len(fetched),
            "gaps": len(self.gaps),
        }
        summary = {
            "project_path": project.path_with_namespace,
            "change_id": record.id,
            "deployment_id": record.deployment_id,
            "environment": payload.get("environment"),
            "ref": payload.get("ref"),
            "sha": payload.get("sha"),
            "finished_at": payload.get("finished_at"),
            "practice": resolved.practice.name if resolved is not None else None,
            "verdict": judgment["verdict"] if judgment else None,
            "changes_basis": basis,
            "reports": {
                "listed": len(reports.reports),
                "fetched": len(fetched),
                "bytes": sum(len(r.data or b"") for r in fetched),
                "jobs_read": reports.jobs,
                "error": reports.error,
            },
            "gaps_by_code": dict(sorted(_count(g["code"] for g in self.gaps).items())),
        }
        files = {"README.txt": self._readme(document, summary, counts), **files}
        return self._write(
            files,
            counts,
            summary,
            {
                "count": len(observation_rows),
                "digest_invalid": invalid,
                "payload_withheld": withheld,
            },
            project,
        )

    def _reports(self, project: GitLabProject, payload: Mapping[str, Any]) -> Fetched:
        pipeline = payload.get("pipeline_id")
        name = f"deployment {payload.get('id')}"
        if not isinstance(pipeline, int):
            self._gap("reports_not_read", name, "GitLab did not record the release's pipeline.")
            return Fetched([])
        if self.fetch is None:
            self._gap("reports_not_requested", name, "Reports were not requested.")
            return Fetched([])
        fetched = self.fetch(project.gitlab_id, pipeline)
        if fetched.error is not None:
            self._gap("reports_not_read", f"pipeline {pipeline}", f"Reports: {fetched.error}.")
        return fetched

    def _change_row(
        self, record: ChangeRecord, observation: Observation, this_change: ChangeRecord | None
    ) -> dict[str, Any]:
        self._use(observation)
        payload = _dict(observation.payload)
        approvals = _dict(payload.get("approvals"))
        pipeline = _dict(payload.get("pipeline"))
        tests = _dict(pipeline.get("tests"))
        approved_by = [
            str(a["username"])
            for a in approvals.get("approved_by") or []
            if isinstance(a, dict) and isinstance(a.get("username"), str)
        ]
        row: dict[str, Any] = redact_value(
            {
                "change_id": record.id,
                "kind": record.kind,
                "iid": record.iid,
                "sha": record.sha,
                "title": payload.get("title"),
                "author": payload.get("author"),
                "merged_by": payload.get("merged_by"),
                "merged_at": iso(record.changed_at),
                "approved_by": ";".join(approved_by),
                "self_approved": approvals.get("self_approved"),
                "review_outcome": record.review_outcome,
                "outcome": record.outcome,
                "pipeline_id": pipeline.get("id"),
                "pipeline_status": pipeline.get("status"),
                "tests_status": pipeline.get("tests_status"),
                "tests_total": tests.get("total"),
                "tests_failed": tests.get("failed"),
                "shipped": this_change is not None and this_change.id == record.id,
                "web_path": payload.get("web_path"),
                "observation_id": observation.id,
            }
        )
        return row

    def _observation_row(
        self, observation: Observation, project: GitLabProject
    ) -> tuple[dict[str, Any], bool, bool]:
        payload = observation.payload
        withheld = payload is not None and redact_value(payload) != payload
        valid = digest_valid(observation)
        row: dict[str, Any] = redact_value(
            {
                "id": observation.id,
                "project_gitlab_id": project.gitlab_id,
                "project_path": project.path_with_namespace,
                "source": observation.source,
                "source_ref": observation.source_ref,
                "collected_at": iso(observation.collected_at),
                "collection_status": observation.collection_status,
                "payload_digest": observation.payload_digest,
                "digest_valid": valid,
                "payload_withheld": withheld,
                "error": observation.error,
            }
        )
        row["payload"] = None if withheld else payload
        if not valid:
            self._gap(
                "digest_invalid",
                f"observation {observation.id}",
                "The stored observation no longer matched its digest when exported.",
            )
        return row, withheld, valid

    # Output -------------------------------------------------------------------------------------

    def _readme(
        self, document: Mapping[str, Any], summary: Mapping[str, Any], counts: Mapping[str, int]
    ) -> bytes:
        deployment = document["deployment"]
        roles = document["roles"]
        approvers = ", ".join(
            f"{a.get('username')} ({a.get('status')} {a.get('created_at')})"
            for a in roles.get("approvals") or []
            if isinstance(a, dict)
        )
        judgment = document["judgment"]
        lines = [
            "Baluarte audit release",
            "",
            f"Export ID:     {self.export_id}",
            f"Generated at:  {self.now.isoformat()}",
            f"Project:       {document['project']['path']}",
            f"Environment:   {deployment['environment']}",
            f"Deployed:      {deployment['ref']} at {deployment['sha']}",
            f"Finished at:   {deployment['finished_at']}",
            "",
            "Who did what (as GitLab records it)",
            f"  Started by:  {roles['started_by'] or 'not known'}",
            f"  Approved by: {approvers or 'nobody recorded'}",
            f"  Deployed by: {roles['deployer'] or 'not known'}",
            "",
            f"Practice:      {summary['practice'] or 'none applies'}",
            f"Judgment:      {judgment['verdict'] if judgment else 'not judged'}",
        ]
        if judgment:
            lines += [f"  - {reason}" for reason in judgment["reasons"]]
        lines += ["", "Counts", *[f"  {name}: {value}" for name, value in counts.items()]]
        lines += ["", "Gaps"]
        if self.gaps:
            lines += [f"  {k}: {v}" for k, v in summary["gaps_by_code"].items()]
            lines.append("  Each is listed in gaps.csv.")
        else:
            lines.append("  None.")
        lines += ["", "Contents", *[f"  {line}" for line in CONTENTS]]
        lines += ["", "Limitations", *[f"  - {line}" for line in LIMITATIONS]]
        lines += ["", "Verifying", *[f"  - {line}" for line in VERIFICATION], ""]
        text: str = redact_value("\n".join(lines))
        return text.encode()

    def _write(
        self,
        files: Mapping[str, bytes],
        counts: dict[str, int],
        summary: dict[str, Any],
        observations: dict[str, Any],
        project: GitLabProject,
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
                "scope": {"kind": "release", "change_id": self.change_id},
                "requested_by": self.requested_by,
                "digests": DIGESTS,
                "files": entries,
                "counts": counts,
                "observations": observations,
                "release_export": summary,
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
        slug = project.path_with_namespace.replace("/", "-")
        return Built(
            file=sink,
            filename=f"baluarte-release-{slug}-{summary['deployment_id']}-{stamp}.zip",
            size=size,
            sha256=whole.hexdigest(),
            manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest(),
            counts=counts,
            files={e["name"]: e for e in entries if not e["name"].startswith("reports/")},
        )


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def build(
    db: Session,
    change_id: int,
    export_id: str,
    requested_by: str,
    now: datetime,
    fetch: Fetcher | None,
) -> Built:
    return Builder(db, change_id, export_id, requested_by, now, fetch).build()
