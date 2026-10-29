"""Export manifests (BAL-028) and the offline verifier, ``baluarte-verify-export``."""

import contextlib
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from gitlab_fake import TOKEN
from sqlalchemy import func, insert, select, text
from sqlalchemy.orm import Session
from test_evaluation_store import VISIBILITY_ONLY, World
from test_evidence_export import (
    FAKE_PAT,
    SECTIONS,
    audit_events,
    export_zip,
    gitlab_env,
    post,
    world,
)

from baluarte.db.models import CollectionRun, GitLabProject
from baluarte.evidence import verify

pytestmark = pytest.mark.db

__all__ = ["gitlab_env", "world"]

ZIP_FILES = [
    "README.txt",
    "export.json",
    "inventory.csv",
    "evaluations.csv",
    "findings.csv",
    "observations.jsonl",
    "policies.csv",
    "collection_errors.csv",
    "collection_runs.csv",
]


def canonical_sha256(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def seeded(world: World) -> None:
    """Two evaluated projects (fail, pass), one never observed (unknown), one missing."""
    world.project(1, "public")
    world.project(2, "private")
    world.project(3, None, observe=False)
    world.project(4, "private", state="missing")
    world.run()


def save(tmp_path: Path, name: str, content: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def rezip(original: bytes, changes: dict[str, bytes]) -> bytes:
    """A copy of the ZIP with some entries replaced, keeping their order."""
    source = zipfile.ZipFile(io.BytesIO(original))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info, changes.get(info.filename, source.read(info.filename)))
    return buffer.getvalue()


def resum(original: bytes, changes: dict[str, bytes]) -> bytes:
    """Replace entries and rewrite the manifest and SHA256SUMS to match, as a forger would."""
    archive = zipfile.ZipFile(io.BytesIO(original))
    manifest = json.loads(changes.get("manifest.json", archive.read("manifest.json")))
    for entry in manifest["files"]:
        if entry["name"] in changes:
            data = changes[entry["name"]]
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["bytes"] = len(data)
    changes = {**changes, "manifest.json": json.dumps(manifest).encode()}
    sums = "".join(
        f"{hashlib.sha256(changes.get(name, archive.read(name))).hexdigest()}  {name}\n"
        for name in archive.namelist()
        if name != "SHA256SUMS"
    )
    return rezip(original, {**changes, "SHA256SUMS": sums.encode()})


def run(path: str, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        status = verify.main([path, *args])
    return status, out.getvalue()


def test_a_zip_ends_with_a_manifest_and_sha256sums(world: World) -> None:
    seeded(world)

    response, archive = export_zip(world.engine, all=True)

    assert archive.namelist() == [*ZIP_FILES, "manifest.json", "SHA256SUMS"]
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["manifest_format"] == "baluarte.evidence-manifest/v1"
    assert manifest["export_format"] == "baluarte.evidence-export/v1"
    assert manifest["package"] == "zip"
    assert manifest["export_id"] == response.headers["x-baluarte-export-id"]
    assert manifest["generated_at"] == json.loads(archive.read("export.json"))["generated_at"]
    assert manifest["baluarte_version"]
    assert manifest["scope"] == {"all": True}
    # The internal user ID, not a name or email.
    assert manifest["requested_by"] == "user:1"

    assert [entry["name"] for entry in manifest["files"]] == ZIP_FILES
    for entry in manifest["files"]:
        data = archive.read(entry["name"])
        assert entry["sha256"] == hashlib.sha256(data).hexdigest(), entry["name"]
        assert entry["bytes"] == len(data)
    rows = {entry["name"]: entry["rows"] for entry in manifest["files"]}
    assert rows["evaluations.csv"] == 3
    assert rows["inventory.csv"] == 4
    assert manifest["counts"]["evaluations"] == 3

    sums = archive.read("SHA256SUMS").decode().splitlines()
    assert [line.split("  ")[1] for line in sums] == [*ZIP_FILES, "manifest.json"]
    for line in sums:
        digest, name = line.split("  ")
        assert digest == hashlib.sha256(archive.read(name)).hexdigest()


def test_the_manifest_summarises_results_evidence_and_collection(world: World) -> None:
    seeded(world)
    with Session(world.engine) as db:
        project = db.scalars(select(GitLabProject).where(GitLabProject.gitlab_id == 2)).one()
        project.last_error = "could not confirm: GitLab returned 502 for /projects/102"
        db.commit()
    world.collection("failed")

    _, archive = export_zip(world.engine, all=True)
    manifest = json.loads(archive.read("manifest.json"))

    assert manifest["evaluations"]["outcomes"] == {"fail": 1, "pass": 1, "unknown": 1}
    assert sum(manifest["evaluations"]["unknown_reasons"].values()) >= 1
    (policy,) = manifest["policies"]
    assert policy["name"] == "default-gitlab-baseline"
    assert policy["yaml_digest"] == hashlib.sha256(VISIBILITY_ONLY.encode()).hexdigest()
    assert manifest["observations"]["count"] == 2
    assert manifest["observations"]["digest_invalid"] == 0
    assert manifest["observations"]["by_collection_status"] == {"ok": 2}
    errors = {
        (e["resource_type"], e["state"]): e["count"]
        for e in manifest["collection_errors"]["resources"]
    }
    assert errors == {("project", "active"): 1, ("project", "missing"): 1}
    assert manifest["collection_errors"]["with_error_message"] == 1
    failed = [run for run in manifest["runs_not_succeeded"] if run["status"] == "failed"]
    assert [(run["run_type"], run["kind"]) for run in failed] == [("collection", "projects")]
    assert manifest["freshness"]["collection"]["kinds"]["projects"]["status"] == "failed"
    assert manifest["freshness"]["evaluation"]["status"]
    limitations = " ".join(manifest["limitations"])
    assert "not a SOC 2 certification" in limitations
    assert "not anchored outside the database" in limitations


def test_a_json_export_hashes_each_section_and_its_metadata(world: World) -> None:
    seeded(world)

    document = post(world.engine, {"format": "json", "scope": {"all": True}}).json()

    manifest = document["manifest"]
    assert manifest["package"] == "json"
    assert [s["name"] for s in manifest["sections"]] == SECTIONS
    for section in manifest["sections"]:
        rows = document[section["name"]]
        digest = hashlib.sha256()
        for row in rows:
            digest.update(
                json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                + b"\n"
            )
        assert section["sha256"] == digest.hexdigest(), section["name"]
        assert section["rows"] == len(rows)
    metadata = {key: document[key] for key in verify.METADATA}
    assert manifest["metadata_sha256"] == canonical_sha256(metadata)
    assert manifest["counts"] == document["counts"]


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_the_manifest_hash_is_recorded_in_the_audit_event(world: World, fmt: str) -> None:
    seeded(world)

    response = post(world.engine, {"format": fmt, "scope": {"all": True}})

    if fmt == "json":
        manifest = response.json()["manifest"]
    else:
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(response.content)).read("manifest.json"))
    _, completed = audit_events(world.engine)
    assert completed.details["manifest_sha256"] == canonical_sha256(manifest)
    if fmt == "csv":
        assert set(completed.details["files"]) >= {"manifest.json", "SHA256SUMS"}
    else:
        assert set(completed.details["sections"]) == set(SECTIONS)


def test_no_credentials_in_the_manifest(world: World) -> None:
    seeded(world)
    with Session(world.engine) as db:
        db.execute(
            insert(CollectionRun).values(
                instance_id=world.instance_id,
                kind="groups",
                status="failed",
                started_at=func.now(),
                finished_at=func.now(),
                error=f"GitLab said: private_token={FAKE_PAT} and {TOKEN}",
            )
        )
        db.commit()

    _, archive = export_zip(world.engine, all=True)
    manifest = archive.read("manifest.json")

    assert FAKE_PAT.encode() not in manifest
    assert TOKEN.encode() not in manifest
    assert any(run["kind"] == "groups" for run in json.loads(manifest)["runs_not_succeeded"])


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_the_verifier_passes_a_real_export(world: World, tmp_path: Path, fmt: str) -> None:
    seeded(world)
    response = post(world.engine, {"format": fmt, "scope": {"all": True}})
    path = save(tmp_path, f"export.{'json' if fmt == 'json' else 'zip'}", response.content)
    _, completed = audit_events(world.engine)

    status, output = run(path, "--manifest-sha256", completed.details["manifest_sha256"])

    assert status == 0, output
    assert "OK:" in output and "FAIL" not in output
    assert f"manifest sha256 {completed.details['manifest_sha256']}" in output


def test_the_verifier_notes_an_unchecked_manifest(world: World, tmp_path: Path) -> None:
    seeded(world)
    response, _ = export_zip(world.engine, all=True)

    status, output = run(save(tmp_path, "export.zip", response.content))

    assert status == 0, output
    assert "NOTE the manifest itself was not checked" in output


def test_the_verifier_fails_an_edited_file(world: World, tmp_path: Path) -> None:
    seeded(world)
    response, archive = export_zip(world.engine, all=True)
    edited = archive.read("evaluations.csv").replace(b",fail,", b",pass,")

    status, output = run(
        save(tmp_path, "export.zip", rezip(response.content, {"evaluations.csv": edited}))
    )

    assert status == 1
    assert "FAIL evaluations.csv: does not match SHA256SUMS" in output
    assert "FAIL evaluations.csv: does not match the manifest" in output


def test_the_verifier_fails_an_added_or_removed_file(world: World, tmp_path: Path) -> None:
    seeded(world)
    response, _ = export_zip(world.engine, all=True)
    buffer = io.BytesIO(response.content)
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("extra.csv", "a,b\r\n")

    status, output = run(save(tmp_path, "export.zip", buffer.getvalue()))

    assert status == 1
    assert "FAIL extra.csv: not listed in SHA256SUMS" in output
    assert "FAIL extra.csv: not listed in the manifest" in output


def test_the_verifier_checks_each_observation_digest(world: World, tmp_path: Path) -> None:
    seeded(world)
    response, archive = export_zip(world.engine, all=True)
    edited = archive.read("observations.jsonl").replace(b'"public"', b'"private"')

    # Every file hash is rewritten to match; only the observation digest still catches it.
    status, output = run(
        save(tmp_path, "export.zip", resum(response.content, {"observations.jsonl": edited}))
    )

    assert status == 1
    assert "payload does not match payload_digest" in output


def test_only_the_audit_event_catches_a_rewritten_manifest(world: World, tmp_path: Path) -> None:
    seeded(world)
    response, archive = export_zip(world.engine, all=True)
    _, completed = audit_events(world.engine)
    manifest = json.loads(archive.read("manifest.json"))
    manifest["evaluations"]["outcomes"] = {"pass": 3}
    path = save(
        tmp_path,
        "export.zip",
        resum(response.content, {"manifest.json": json.dumps(manifest).encode()}),
    )

    assert run(path)[0] == 0
    status, output = run(path, "--manifest-sha256", completed.details["manifest_sha256"])

    assert status == 1
    assert "FAIL manifest: SHA-256 differs from the audit event" in output


def test_the_verifier_fails_an_edited_json_export(world: World, tmp_path: Path) -> None:
    seeded(world)
    document = post(world.engine, {"format": "json", "scope": {"all": True}}).json()
    failed = next(e for e in document["evaluations"] if e["outcome"] == "fail")
    failed["outcome"] = "pass"
    document["scope"] = {"projects": [2]}

    status, output = run(save(tmp_path, "export.json", json.dumps(document).encode()))

    assert status == 1
    assert "FAIL evaluations: does not match the manifest" in output
    assert "FAIL metadata: does not match the manifest" in output


def test_evidence_invalid_at_export_is_a_warning_not_a_failure(
    world: World, tmp_path: Path
) -> None:
    project_id = world.project(1, "private")
    world.run()
    with world.engine.begin() as connection:
        # A privileged change that bypasses the trigger, before the export.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text(
                "UPDATE observation SET payload = jsonb_set(payload, '{visibility}', "
                "'\"public\"') WHERE project_id = :project_id"
            ),
            {"project_id": project_id},
        )

    response, archive = export_zip(world.engine, all=True)
    manifest = json.loads(archive.read("manifest.json"))
    status, output = run(save(tmp_path, "export.zip", response.content))

    assert manifest["observations"]["digest_invalid"] == 1
    assert len(manifest["observations"]["digest_invalid_ids"]) == 1
    assert status == 0, output
    assert "WARN observation" in output and "digest_valid false" in output


def test_the_verifier_rejects_what_it_cannot_read(tmp_path: Path) -> None:
    assert run(str(tmp_path / "absent.zip"))[0] == 2
    assert run(save(tmp_path, "not.json", b"{not json"))[0] == 2
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.txt", "hello")
    status, output = run(save(tmp_path, "partial.zip", buffer.getvalue()))
    assert status == 2
    assert "manifest.json is missing" in output
