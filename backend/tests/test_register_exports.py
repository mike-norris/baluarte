"""Issue register export packages, verified offline (roadmap M12.4)."""

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import FakeGitLab
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_control_mapping import client
from test_issue_evidence import indexed as indexed  # the fixture
from test_issue_mappings import analyst, propose
from test_issue_mappings import captured as captured  # the fixture
from test_issue_register import world as world  # the fixture

from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent
from baluarte.evidence.verify import canonical, verify

pytestmark = pytest.mark.db

EXPORTS = "/api/v1/register/exports"


def rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(archive.read(name).decode(), newline="")))


def download(http: TestClient, body: dict[str, Any], tmp_path: Path) -> tuple[Path, str]:
    response = http.post(EXPORTS, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    path = tmp_path / "package.zip"
    path.write_bytes(response.content)
    return path, response.headers["x-baluarte-export-id"]


def period(http: TestClient) -> int:
    created = http.post(
        "/api/v1/register/periods",
        json={"name": "SOC 2 2026", "starts_on": "2026-10-01", "ends_on": "2026-11-01"},
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def test_one_issue_exports_its_mappings_evidence_and_files_and_verifies(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab, tmp_path: Path
) -> None:
    project = world["project"]
    http = analyst(migrated_engine)
    period_id = period(http)
    attachment = next(
        i["key"]
        for i in http.get(f"/api/v1/register/issues/{project}/1/mappings").json()["citable"]
        if i["key"].startswith("attachment:")
    )
    inside = propose(migrated_engine, project, ["event:label:2", attachment]).json()
    later = propose(migrated_engine, project, ["note:11"], control_id="CC6.1").json()

    path, export_id = download(
        http, {"issues": [{"project_id": project, "iid": 1}], "period_id": period_id}, tmp_path
    )
    report, manifest = verify(str(path))
    assert report.failures == []

    with zipfile.ZipFile(path) as archive:
        (issue,) = rows(archive, "issues.csv")
        assert (issue["iid"], issue["auditable"], issue["evidence_status"]) == (
            "1",
            "true",
            "complete",
        )
        assert issue["why"].startswith("rule: ")
        index = {r["mapping_id"]: r for r in rows(archive, "issue_controls.csv")}
        assert set(index) == {str(inside["id"]), str(later["id"])}
        assert index[str(inside["id"])]["framework_edition"].startswith("AICPA")
        assert index[str(inside["id"])]["cited_in_period"] == "2"
        cited = rows(archive, "mapping_evidence.csv")
        timings = {(r["mapping_id"], r["item_key"]): r["timing"] for r in cited}
        assert timings[(str(later["id"]), "note:11")] == "after"
        gaps = {(r["subject"], r["ref"], r["code"]) for r in rows(archive, "gaps.csv")}
        assert ("mapping", str(later["id"]), "only_later_remediation") in gaps
        (stored,) = rows(archive, "files.csv")
        sha = attachment.split(":", 1)[1]
        assert stored["sha256"] == sha
        assert hashlib.sha256(archive.read(f"files/{sha}")).hexdigest() == sha
        # One capture, cited by both mappings, is stored once.
        assert len(archive.read("observations.jsonl").splitlines()) == 1
        readme = archive.read("README.txt").decode()
        assert "Audit period:  SOC 2 2026 (2026-10-01 to 2026-11-01)" in readme
        assert "soc2 CC6.1: active" in readme

    record = http.get(f"{EXPORTS}/{export_id}").json()
    assert record["manifest_sha256"] == hashlib.sha256(canonical(manifest)).hexdigest()
    report, _ = verify(str(path), record["manifest_sha256"])
    assert report.failures == []
    with Session(migrated_engine) as db:
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_id == export_id)
        ).all()
    assert actions == ["register.export_created", "register.export_completed"]


def test_controls_export_lists_controls_without_issues_as_gaps(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab, tmp_path: Path
) -> None:
    project = world["project"]
    propose(migrated_engine, project, ["merge_request:42:5"])
    auditor = client(migrated_engine, 7, Role.AUDITOR)
    path, _ = download(
        auditor,
        {
            "controls": [
                {"framework": "soc2", "control_id": "CC8.1"},
                {"framework": "soc2", "control_id": "CC6.1"},
            ]
        },
        tmp_path,
    )
    report, manifest = verify(str(path))
    assert report.failures == []
    assert manifest["register_export"]["kind"] == "controls"
    with zipfile.ZipFile(path) as archive:
        assert [r["iid"] for r in rows(archive, "issues.csv")] == ["1"]
        gaps = {(r["subject"], r["ref"], r["code"]) for r in rows(archive, "gaps.csv")}
        assert ("control", "soc2 CC6.1", "no_active_mapping") in gaps
        assert not any(ref == "soc2 CC8.1" for _, ref, _ in gaps)


def _rewrite(path: Path, name: str, data: bytes) -> None:
    """Replace one file, and fix SHA256SUMS and the manifest so only deeper checks can tell."""
    with zipfile.ZipFile(path) as archive:
        contents = {n: archive.read(n) for n in archive.namelist()}
    contents[name] = data
    manifest = json.loads(contents["manifest.json"])
    for entry in manifest["files"]:
        if entry["name"] == name:
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["bytes"] = len(data)
    contents["manifest.json"] = json.dumps(manifest).encode()
    contents["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in contents.items() if n != "SHA256SUMS"
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for n, d in contents.items():
            archive.writestr(n, d)


def test_the_verifier_catches_a_swapped_file_and_a_broken_index(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab, tmp_path: Path
) -> None:
    project = world["project"]
    http = analyst(migrated_engine)
    attachment = next(
        i["key"]
        for i in http.get(f"/api/v1/register/issues/{project}/1/mappings").json()["citable"]
        if i["key"].startswith("attachment:")
    )
    propose(migrated_engine, project, [attachment])
    path, _ = download(http, {"issues": [{"project_id": project, "iid": 1}]}, tmp_path)
    sha = attachment.split(":", 1)[1]

    _rewrite(path, f"files/{sha}", b"not the original")
    report, _ = verify(str(path))
    assert f"files/{sha}: content does not match its name" in report.failures

    path, _ = download(http, {"issues": [{"project_id": project, "iid": 1}]}, tmp_path)
    _rewrite(path, "issues.csv", b"project_id,iid\n")
    report, _ = verify(str(path))
    assert any("its issue is missing" in failure for failure in report.failures)


def test_an_export_must_name_auditable_issues_or_known_controls(
    migrated_engine: Engine, world: dict[str, int], captured: FakeGitLab
) -> None:
    project = world["project"]
    http = analyst(migrated_engine)
    not_auditable = http.post(EXPORTS, json={"issues": [{"project_id": project, "iid": 2}]})
    assert not_auditable.status_code == 422
    assert not_auditable.json()["detail"]["code"] == "issue_not_auditable"
    unknown = http.post(EXPORTS, json={"controls": [{"framework": "soc2", "control_id": "ZZ9"}]})
    assert unknown.json()["detail"]["code"] == "control_unknown"
    no_period = http.post(
        EXPORTS, json={"issues": [{"project_id": project, "iid": 1}], "period_id": 999}
    )
    assert no_period.json()["detail"]["code"] == "period_unknown"
    assert http.post(EXPORTS, json={}).status_code == 422
    manager = client(migrated_engine, 8, Role.MANAGER)
    assert (
        manager.post(EXPORTS, json={"issues": [{"project_id": project, "iid": 1}]}).status_code
        == 403
    )
