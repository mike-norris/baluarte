"""Reports from a release pipeline, for an audit release (RG.4, docs/RELEASE_GOVERNANCE.md).

Read-only and size-capped. The pipeline's jobs are listed once, and every report artifact of a
type an auditor asks for is listed with its size, whether or not it is fetched:

- ``junit``: GitLab's parsed test report for the pipeline
  (``GET /projects/:id/pipelines/:id/test_report``), once per pipeline;
- ``cyclonedx``, ``sast``, ``dependency_scanning``, ``secret_detection``,
  ``container_scanning``: GitLab's REST API does not serve a report by type, so the file is taken
  from the job's artifacts archive when the job also keeps it there (``artifacts:paths``) and
  the archive is within the limits.

Each report is at most ``MAX_REPORT_BYTES``, and a release at most ``MAX_RELEASE_BYTES`` in
all; anything else is listed with why it was not fetched, never silently left out. Redirects
(artifacts in object storage served by redirect) are not followed. Source code is never fetched:
only files named like a report are read out of an archive.
"""

import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from baluarte.integrations.gitlab.changes import parse_time
from baluarte.integrations.gitlab.client import (
    GitLabClient,
    GitLabError,
    Redirected,
    TooLarge,
    Unreachable,
)

REPORT_TYPES = (
    "junit",
    "cyclonedx",
    "sast",
    "dependency_scanning",
    "secret_detection",
    "container_scanning",
)
MAX_REPORT_BYTES = 10 * 1024 * 1024
MAX_RELEASE_BYTES = 50 * 1024 * 1024
MAX_JOBS = 200

STATUS_TEXT = {
    "fetched": "Fetched.",
    "too_large": f"Larger than {MAX_REPORT_BYTES // (1024 * 1024)} MB; listed, not fetched.",
    "over_release_limit": (
        f"The release's reports already reach {MAX_RELEASE_BYTES // (1024 * 1024)} MB; listed, "
        "not fetched."
    ),
    "no_archive": "The job keeps the report only as a report artifact, which GitLab's API does "
    "not serve by type; listed, not fetched.",
    "not_in_archive": "The job's artifacts archive does not hold the report file.",
    "archive_too_large": "The job's artifacts archive is too large to read for the report.",
    "redirected": "GitLab serves the artifact by redirect (object storage); not followed.",
    "expired": "The artifact has expired or was removed in GitLab.",
    "failed": "GitLab refused or failed the read.",
    "unreachable": "GitLab could not be reached.",
    "in_test_report": "Included in the pipeline's parsed test report (test_report.json).",
}


@dataclass(frozen=True)
class Report:
    file_type: str
    job_id: int | None
    job_name: str | None
    filename: str | None
    # The size GitLab lists for the artifact; the fetched file's own size is ``len(data)``.
    listed_bytes: int | None
    status: str
    data: bytes | None = None
    expire_at: str | None = None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.data).hexdigest() if self.data is not None else None


@dataclass(frozen=True)
class Fetched:
    reports: list[Report]
    # Why the jobs could not be listed, when they could not; None when they were.
    error: str | None = None
    jobs: int = 0


def _status(error: GitLabError) -> str:
    if isinstance(error, Redirected):
        return "redirected"
    if isinstance(error, TooLarge):
        return "too_large"
    if isinstance(error, Unreachable):
        return "unreachable"
    if getattr(error, "status", None) in (404, 410):
        return "expired"
    return "failed"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _member(archive: zipfile.ZipFile, filename: str) -> zipfile.ZipInfo | None:
    """The archive member named like the report, at any depth; None when there is none."""
    names = {filename, filename.removesuffix(".gz")}
    for info in archive.infolist():
        if not info.is_dir() and posixpath.basename(info.filename) in names:
            return info
    return None


class _Budget:
    def __init__(self) -> None:
        self.used = 0

    def fits(self, size: int) -> bool:
        return self.used + size <= MAX_RELEASE_BYTES

    def take(self, size: int) -> None:
        self.used += size


def fetch(client: GitLabClient, project: int, pipeline: int, now: datetime) -> Fetched:
    """Every wanted report of the pipeline, fetched within the limits or listed with why not."""
    try:
        jobs: list[dict[str, Any]] = []
        for page in client.paginate(
            f"/projects/{project}/pipelines/{pipeline}/jobs", {"include_retried": "false"}
        ):
            jobs += page
            if len(jobs) >= MAX_JOBS:
                jobs = jobs[:MAX_JOBS]
                break
    except GitLabError as error:
        return Fetched([], f"the pipeline's jobs could not be listed ({_status(error)})")
    budget = _Budget()
    reports: list[Report] = []
    junit: list[Report] = []
    for job in sorted(jobs, key=lambda j: _int(j.get("id")) or 0):
        artifacts = [a for a in job.get("artifacts") or [] if isinstance(a, dict)]
        wanted = [a for a in artifacts if a.get("file_type") in REPORT_TYPES]
        if not wanted:
            continue
        job_id = _int(job.get("id"))
        expire_at = job.get("artifacts_expire_at")
        expired = (found := parse_time(expire_at)) is not None and found < now
        listed = [
            Report(
                file_type=a["file_type"],
                job_id=job_id,
                job_name=job.get("name") if isinstance(job.get("name"), str) else None,
                filename=a.get("filename") if isinstance(a.get("filename"), str) else None,
                listed_bytes=_int(a.get("size")),
                status="expired" if expired else "pending",
                expire_at=expire_at if isinstance(expire_at, str) else None,
            )
            for a in wanted
        ]
        junit += [r for r in listed if r.file_type == "junit"]
        others = [r for r in listed if r.file_type != "junit"]
        if others and not expired and job_id is not None:
            archive = next((a for a in artifacts if a.get("file_type") == "archive"), None)
            others = _from_archive(client, project, job_id, archive, others, budget)
        reports += [r for r in listed if r.file_type == "junit"] + others
    if junit:
        reports = _test_report(client, project, pipeline, reports, junit, budget)
    return Fetched(
        [replace(r, status="failed") if r.status == "pending" else r for r in reports],
        jobs=len(jobs),
    )


def _from_archive(
    client: GitLabClient,
    project: int,
    job_id: int,
    archive: dict[str, Any] | None,
    reports: list[Report],
    budget: _Budget,
) -> list[Report]:
    if archive is None:
        return [replace(r, status="no_archive") for r in reports]
    size = _int(archive.get("size"))
    if size is None or size > MAX_RELEASE_BYTES or not budget.fits(size):
        return [replace(r, status="archive_too_large") for r in reports]
    try:
        body = client.download(f"/projects/{project}/jobs/{job_id}/artifacts", MAX_RELEASE_BYTES)
    except GitLabError as error:
        status = _status(error)
        return [
            replace(r, status="archive_too_large" if status == "too_large" else status)
            for r in reports
        ]
    out = []
    try:
        opened = zipfile.ZipFile(io.BytesIO(body.content))
    except zipfile.BadZipFile:
        return [replace(r, status="failed") for r in reports]
    with opened:
        for report in reports:
            member = _member(opened, report.filename) if report.filename else None
            if member is None:
                out.append(replace(report, status="not_in_archive"))
            elif member.file_size > MAX_REPORT_BYTES:
                out.append(replace(report, status="too_large"))
            elif not budget.fits(member.file_size):
                out.append(replace(report, status="over_release_limit"))
            else:
                data = opened.read(member)
                budget.take(len(data))
                out.append(replace(report, status="fetched", data=data))
    return out


def _test_report(
    client: GitLabClient,
    project: int,
    pipeline: int,
    reports: list[Report],
    junit: list[Report],
    budget: _Budget,
) -> list[Report]:
    """The pipeline's parsed test report, once, standing for every junit artifact."""
    live = [r for r in junit if r.status == "pending"]
    if not live:
        return reports
    status = "fetched"
    data: bytes | None = None
    limit = min(MAX_REPORT_BYTES, MAX_RELEASE_BYTES - budget.used)
    if limit <= 0:
        status = "over_release_limit"
    else:
        try:
            data = client.download(
                f"/projects/{project}/pipelines/{pipeline}/test_report", limit
            ).content
        except GitLabError as error:
            status = _status(error)
            if status == "too_large" and limit < MAX_REPORT_BYTES:
                status = "over_release_limit"
        else:
            budget.take(len(data))
    combined = Report(
        file_type="junit",
        job_id=None,
        job_name=None,
        filename="test_report.json",
        listed_bytes=sum(r.listed_bytes or 0 for r in junit),
        status=status,
        data=data if status == "fetched" else None,
    )
    # Each junit artifact stays listed; the parsed report is one file for the pipeline.
    marked = [replace(r, status="in_test_report") if r.status == "pending" else r for r in reports]
    return [*marked, combined]
