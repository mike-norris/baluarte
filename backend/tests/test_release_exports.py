"""Audit release packages: one production release and its evidence, verified offline (RG.4)."""

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_change_collection import PROJECT, ago, approved, mr, sha, tag_release_repository
from test_control_mapping import client
from test_release_checks import Releases
from test_release_checks import gitlab_env as gitlab_env  # the fixture

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, ChangeRecord
from baluarte.evidence.verify import verify
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.releases import reports

EXPORTS = "/api/v1/releases/exports"
PIPELINE = 500
SAST = json.dumps({"version": "15.0", "vulnerabilities": []}).encode()
TEST_REPORT = json.dumps({"total_count": 3, "failed_count": 0, "test_suites": []}).encode()
SOURCE = b"print('source code never leaves GitLab')\n"


def archive(**members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, data in members.items():
            zipped.writestr(name.replace("__", "/"), data)
    return buffer.getvalue()


class Pipeline:
    """A release pipeline's jobs and artifacts, served read-only."""

    def __init__(self) -> None:
        self.sast_archive = archive(**{"gl-sast-report.json": SAST, "src__app.py": SOURCE})
        self.jobs: list[dict[str, Any]] = [
            {
                "id": 900,
                "name": "test",
                "artifacts": [
                    {"file_type": "junit", "size": 300, "filename": "junit.xml.gz"},
                    {"file_type": "trace", "size": 10, "filename": "job.log"},
                ],
            },
            {
                "id": 901,
                "name": "sast",
                "artifacts": [
                    {"file_type": "sast", "size": len(SAST), "filename": "gl-sast-report.json"},
                    {
                        "file_type": "archive",
                        "size": len(self.sast_archive),
                        "filename": "artifacts.zip",
                    },
                ],
            },
            {
                "id": 902,
                "name": "sbom",
                "artifacts": [
                    {"file_type": "cyclonedx", "size": 2000, "filename": "gl-sbom.cdx.json"}
                ],
            },
        ]
        self.responses: dict[str, httpx.Response] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        base = f"/api/v4/projects/{PROJECT}"
        if path in self.responses:
            return self.responses[path]
        if path == f"{base}/pipelines/{PIPELINE}/jobs":
            return httpx.Response(200, json=self.jobs)
        if path == f"{base}/jobs/901/artifacts":
            return httpx.Response(200, content=self.sast_archive)
        if path == f"{base}/pipelines/{PIPELINE}/test_report":
            return httpx.Response(200, content=TEST_REPORT)
        return httpx.Response(404, json={"message": "404 Not Found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def fetch(self) -> reports.Fetched:
        settings = GitLabSettings(url=URL, token=TOKEN)
        with GitLabClient(settings, transport=self.transport) as gitlab:
            return reports.fetch(gitlab, PROJECT, PIPELINE, datetime.now(UTC))


def by_type(fetched: reports.Fetched) -> dict[str, list[reports.Report]]:
    found: dict[str, list[reports.Report]] = {}
    for report in fetched.reports:
        found.setdefault(report.file_type, []).append(report)
    return found


# --- reports ---


def test_reports_are_fetched_by_type_and_the_rest_listed_with_why() -> None:
    pipeline = Pipeline()

    fetched = by_type(pipeline.fetch())

    junit = {r.status: r for r in fetched["junit"]}
    assert junit["fetched"].data == TEST_REPORT
    assert junit["fetched"].filename == "test_report.json"
    assert junit["in_test_report"].job_id == 900
    assert [(r.status, r.data) for r in fetched["sast"]] == [("fetched", SAST)]
    assert [r.status for r in fetched["cyclonedx"]] == ["no_archive"]
    assert "trace" not in fetched
    # Only the report's own file is read out of the archive, never the source beside it.
    assert all(r.data != SOURCE for r in pipeline.fetch().reports)


def test_reports_are_capped_and_never_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = Pipeline()
    pipeline.responses[f"/api/v4/projects/{PROJECT}/jobs/901/artifacts"] = httpx.Response(
        302, headers={"Location": "https://objects.example.test/a.zip"}
    )
    assert [r.status for r in by_type(pipeline.fetch())["sast"]] == ["redirected"]

    monkeypatch.setattr(reports, "MAX_REPORT_BYTES", 10)
    pipeline = Pipeline()
    fetched = by_type(pipeline.fetch())
    assert [r.status for r in fetched["sast"]] == ["too_large"]
    assert {r.status for r in fetched["junit"]} == {"in_test_report", "too_large"}


def test_expired_artifacts_and_unlisted_jobs_are_reported() -> None:
    pipeline = Pipeline()
    pipeline.jobs[1]["artifacts_expire_at"] = "2020-01-01T00:00:00Z"
    assert [r.status for r in by_type(pipeline.fetch())["sast"]] == ["expired"]

    pipeline = Pipeline()
    pipeline.responses[f"/api/v4/projects/{PROJECT}/pipelines/{PIPELINE}/jobs"] = httpx.Response(
        403, json={"message": "403 Forbidden"}
    )
    fetched = pipeline.fetch()
    assert fetched.reports == []
    assert fetched.error == "the pipeline's jobs could not be listed (failed)"


# --- the package end to end ---


def rows(zipped: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(zipped.read(name).decode(), newline="")))


def auditor(engine: Engine, pipeline: Pipeline | None = None) -> TestClient:
    http = client(engine, 7, Role.AUDITOR)
    found = pipeline or Pipeline()
    http.app.dependency_overrides[gitlab_transport] = lambda: found.transport  # type: ignore[attr-defined]
    return http


def deployment_id(engine: Engine) -> int:
    with Session(engine) as db:
        return db.scalars(select(ChangeRecord.id).where(ChangeRecord.kind == "deployment")).one()


def download(http: TestClient, body: dict[str, Any], tmp_path: Path) -> tuple[Path, str]:
    response = http.post(EXPORTS, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    path = tmp_path / "release.zip"
    path.write_bytes(response.content)
    return path, response.headers["x-baluarte-export-id"]


@pytest.mark.db
def test_an_audit_release_holds_the_release_its_people_changes_and_reports(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    world = Releases(migrated_engine)
    world.practice()
    world.protections(["*-release"])
    http = auditor(migrated_engine)

    listed = http.get("/api/v1/releases")
    assert listed.status_code == 200, listed.text
    (release,) = listed.json()
    assert (release["ref"], release["started_by"], release["approvers"], release["deployer"]) == (
        "v1-release",
        "dev",
        ["lead"],
        "dev",
    )

    path, export_id = download(http, {"change_id": release["change_id"]}, tmp_path)

    record = http.get(f"{EXPORTS}/{export_id}").json()
    report, manifest = verify(str(path), record["manifest_sha256"])
    assert report.failures == [], report.failures
    assert manifest["release_export"]["verdict"] == "pass"
    with zipfile.ZipFile(path) as zipped:
        names = set(zipped.namelist())
        document = json.loads(zipped.read("release.json"))
        assert document["roles"]["started_by"] == "dev"
        assert document["roles"]["deployer"] == "dev"
        assert [a["username"] for a in document["roles"]["approvals"]] == ["lead"]
        assert document["practice"]["name"] == "tag-releases"
        assert document["judgment"] == {
            "verdict": "pass",
            "reasons": document["judgment"]["reasons"],
        }
        assert document["protections"]["protected_tags"]["items"][0]["name"] == "*-release"
        assert zipped.read("release_notes.md") == b"Fixes and features"
        changes = rows(zipped, "merge_requests.csv")
        assert [(c["kind"], c["iid"], c["shipped"]) for c in changes] == [
            ("merge_request", "1", "True")
        ]
        assert changes[0]["approved_by"] == "reviewer"
        listed_reports = {(r["file_type"], r["status"]) for r in rows(zipped, "reports.csv")}
        assert ("sast", "fetched") in listed_reports
        assert ("cyclonedx", "no_archive") in listed_reports
        assert f"reports/{hashlib.sha256(SAST).hexdigest()}" in names
        assert f"reports/{hashlib.sha256(TEST_REPORT).hexdigest()}" in names
        assert all(zipped.read(n) != SOURCE for n in names)
        gaps = {g["code"] for g in rows(zipped, "gaps.csv")}
        assert {"first_release", "report_no_archive", "release_evidence_not_fetched"} <= gaps
    with Session(migrated_engine) as db:
        actions = [
            e.action
            for e in db.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_id == export_id)
                .order_by(AuditEvent.id)
            )
        ]
    assert actions == ["release.export_created", "release.export_completed"]


@pytest.mark.db
def test_a_tampered_audit_release_fails_verification(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    world = Releases(migrated_engine)
    world.practice()
    world.protections(["*-release"])
    path, _ = download(
        auditor(migrated_engine), {"change_id": deployment_id(migrated_engine)}, tmp_path
    )
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(path) as original, zipfile.ZipFile(tampered, "w") as copy:
        for item in original.infolist():
            data = original.read(item)
            if item.filename == "release.json":
                data = data.replace(b'"dev"', b'"someone-else"')
            copy.writestr(item, data)

    report, _ = verify(str(tampered))

    assert "release.json: does not match SHA256SUMS" in report.failures


@pytest.mark.db
def test_without_reports_or_practice_the_gaps_say_so(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    pipeline = Pipeline()
    Releases(migrated_engine)
    http = auditor(migrated_engine, pipeline)

    path, _ = download(
        http, {"change_id": deployment_id(migrated_engine), "include_reports": False}, tmp_path
    )

    assert pipeline.requests == []
    report, manifest = verify(str(path))
    assert report.failures == []
    assert manifest["release_export"]["verdict"] is None
    with zipfile.ZipFile(path) as zipped:
        gaps = {g["code"] for g in rows(zipped, "gaps.csv")}
        assert rows(zipped, "reports.csv") == []
    assert {"no_practice", "reports_not_requested", "protections_missing"} <= gaps


@pytest.mark.db
def test_only_production_releases_export_and_auditors_may_build_them(
    migrated_engine: Engine,
) -> None:
    Releases(migrated_engine, tag_release_repository())
    http = auditor(migrated_engine)
    with Session(migrated_engine) as db:
        merge_request = db.scalars(
            select(ChangeRecord.id).where(ChangeRecord.kind == "merge_request")
        ).one()

    refused = http.post(EXPORTS, json={"change_id": merge_request})
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "not_a_release"
    unknown = http.post(EXPORTS, json={"change_id": 999999})
    assert unknown.json()["detail"]["code"] == "release_unknown"
    viewer = client(migrated_engine, 8, Role.MANAGER)
    assert (
        viewer.post(EXPORTS, json={"change_id": deployment_id(migrated_engine)}).status_code == 403
    )


def two_releases() -> Any:
    """Release 300 shipped !1; then !2 and !3 merged; release 301 shipped !3; then !4 merged."""
    repo = tag_release_repository()
    repo.merge_requests[0]["merged_at"] = repo.merge_requests[0]["updated_at"] = ago(days=4)
    for deployment in repo.deployments:
        deployment["created_at"] = deployment["updated_at"] = ago(days=3)
        deployment["deployable"]["finished_at"] = ago(days=3)
    for iid, merged in ((2, ago(days=2, hours=12)), (3, ago(days=2)), (4, ago(hours=1))):
        repo.merge_requests.append(mr(iid, merged=merged, merge_commit=sha(f"m{iid}")))
        repo.approvals[iid] = approved("reviewer")
        repo.commit(sha(f"m{iid}"))
    repo.deploy(301, sha("m3"), ref="v2-release", tag=True, deployer="ops", pipeline_id=501)
    return repo


@pytest.mark.db
def test_the_changes_are_those_since_the_previous_production_release(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    Releases(migrated_engine, two_releases())
    with Session(migrated_engine) as db:
        latest = db.scalars(select(ChangeRecord.id).where(ChangeRecord.deployment_id == 301)).one()
        earlier = db.scalars(select(ChangeRecord.id).where(ChangeRecord.deployment_id == 300)).one()

    path, _ = download(
        auditor(migrated_engine), {"change_id": latest, "include_reports": False}, tmp_path
    )

    with zipfile.ZipFile(path) as zipped:
        document = json.loads(zipped.read("release.json"))
        changes = rows(zipped, "merge_requests.csv")
    assert [(c["iid"], c["shipped"]) for c in changes] == [("2", "False"), ("3", "True")]
    assert document["previous_release"]["change_id"] == earlier
    assert document["changes"]["basis"] == "shipped_commits"


@pytest.mark.db
def test_a_redeploy_lists_what_it_shipped_and_says_nothing_is_new(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    repo = two_releases()
    # The second release deploys the same commit as the first.
    repo.deployments[-1]["sha"] = sha("m1")
    Releases(migrated_engine, repo)
    with Session(migrated_engine) as db:
        latest = db.scalars(select(ChangeRecord.id).where(ChangeRecord.deployment_id == 301)).one()

    path, _ = download(
        auditor(migrated_engine), {"change_id": latest, "include_reports": False}, tmp_path
    )

    with zipfile.ZipFile(path) as zipped:
        changes = rows(zipped, "merge_requests.csv")
        gaps = {g["code"] for g in rows(zipped, "gaps.csv")}
    assert [(c["iid"], c["shipped"]) for c in changes] == [("1", "True")]
    assert "nothing_new" in gaps
