"""Check an evidence export offline: ``baluarte-verify-export <file> [--manifest-sha256 HEX]``.

Standard library only and no database, so an auditor can run it anywhere (also as
``python3 verify.py <file>``, Python 3.9 or later). It checks:

- every file (ZIP) or section (JSON) against the hash and row count in the manifest, and a ZIP
  against ``SHA256SUMS``; no file may be missing or unlisted
- every observation's ``payload_digest`` against its payload, recomputed from canonical JSON
- with ``--manifest-sha256``, the manifest against the value recorded in the
  ``evidence.export_completed`` audit event, the only check an edit of both the package and its
  manifest cannot pass
- for an audit-period evidence package, every observation in ``change_evidence.jsonl`` against
  its digest, and the change sample redrawn from the manifest's seed over ``changes.csv``
- for an issue register export, every ``files/<sha256>`` against its name, every attachment a
  capture cites present or listed as missing, and the index consistent: each mapping's issue,
  cited items and capture are in the package
- for an audit release, every ``reports/<sha256>`` against its name and ``reports.csv``, and every
  observation ``release.json`` and ``merge_requests.csv`` cite present in ``observations.jsonl``

Packages of an older export format are still accepted: a v1 export (before exceptions) is checked
against what its own manifest lists, and only a v2 or later export must contain the exceptions.

Exit status: 0 when every check passes, 1 when any fails, 2 when the file cannot be read.
Warnings (evidence that was already invalid when exported, withheld payloads) do not fail it.
"""

# Annotations are not evaluated, so the file also runs on an auditor's older Python 3.
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import sys
import zipfile
from collections.abc import Iterable
from typing import Any

ROW_FILES = (".csv", ".jsonl")
UNLISTED = ("manifest.json", "SHA256SUMS")
# Contents an export format must have beyond what its manifest lists: exceptions from v2.
REQUIRED = {
    "baluarte.evidence-export/v2": {"zip": "exceptions.csv", "json": "exceptions"},
    "baluarte.evidence-export/v3": {"zip": "exceptions.csv", "json": "exceptions"},
    "baluarte.evidence-export/v4": {"zip": "exceptions.csv", "json": "exceptions"},
    "baluarte.evidence-export/v5": {"zip": "exceptions.csv", "json": "exceptions"},
    "baluarte.evidence-package/v1": {"zip": "exceptions.csv"},
}
# What an audit-period package must hold besides the exceptions.
PACKAGE_FILES = ("changes.csv", "change_sample.csv", "change_evidence.jsonl")
# The change kinds the sample is drawn from (baluarte.evidence.sampling).
SAMPLED_KINDS = ("merge_request", "direct_push")
# What an issue register export must hold.
REGISTER_FILES = (
    "issues.csv",
    "issue_controls.csv",
    "mapping_evidence.csv",
    "decisions.csv",
    "observations.jsonl",
    "files.csv",
    "gaps.csv",
    "redactions.csv",
)
# What an audit release must hold.
RELEASE_FILES = (
    "README.txt",
    "release.json",
    "merge_requests.csv",
    "observations.jsonl",
    "reports.csv",
    "gaps.csv",
)
# The members of a JSON export that are neither sections nor the manifest.
METADATA = ("format", "export_id", "generated_at", "instance", "scope", "freshness")


def canonical(value: Any) -> bytes:
    """Sorted keys, no insignificant whitespace, non-ASCII unescaped, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.passed = 0

    def check(self, ok: bool, message: str) -> bool:
        if ok:
            self.passed += 1
        else:
            self.failures.append(message)
        return ok

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _rows(name: str, data: bytes) -> int:
    if name.endswith(".jsonl"):
        return sum(1 for line in data.splitlines() if line.strip())
    records = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    return max(len(records) - 1, 0)


def _sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            sums[name] = digest
    return sums


def verify_zip(archive: zipfile.ZipFile, report: Report) -> tuple[dict[str, Any], list[Any]]:
    names = archive.namelist()
    for name in UNLISTED:
        if name not in names:
            raise ValueError(f"{name} is missing: this is not a complete Baluarte export")
    contents = {name: archive.read(name) for name in names}
    sums = _sums(contents["SHA256SUMS"].decode("utf-8"))
    for name in names:
        if name == "SHA256SUMS":
            continue
        listed = sums.get(name)
        if report.check(listed is not None, f"{name}: not listed in SHA256SUMS"):
            report.check(listed == sha256(contents[name]), f"{name}: does not match SHA256SUMS")
    for name in sums:
        report.check(name in contents, f"{name}: listed in SHA256SUMS but missing")

    manifest: dict[str, Any] = json.loads(contents["manifest.json"])
    listed_files = {entry["name"]: entry for entry in manifest.get("files", [])}
    for name, data in contents.items():
        if name in UNLISTED:
            continue
        entry = listed_files.get(name)
        if entry is None:
            report.check(False, f"{name}: not listed in the manifest")
            continue
        report.check(entry["sha256"] == sha256(data), f"{name}: does not match the manifest")
        report.check(entry["bytes"] == len(data), f"{name}: size differs from the manifest")
        if name.endswith(ROW_FILES):
            report.check(
                entry["rows"] == _rows(name, data), f"{name}: row count differs from the manifest"
            )
    for name in listed_files:
        report.check(name in contents, f"{name}: listed in the manifest but missing")

    if "export.json" in contents:
        meta = json.loads(contents["export.json"])
        report.check(
            meta.get("export_id") == manifest.get("export_id"),
            "export.json: export ID differs from the manifest",
        )
    observations = [
        json.loads(line)
        for line in contents.get("observations.jsonl", b"").splitlines()
        if line.strip()
    ]
    if isinstance(manifest.get("evidence_package"), dict):
        verify_package(contents, manifest["evidence_package"], report)
    if isinstance(manifest.get("register_export"), dict):
        verify_register(contents, observations, report)
    if isinstance(manifest.get("release_export"), dict):
        verify_release(contents, observations, report)
    return manifest, observations


def draw(population: Iterable[int], size: int, seed: int) -> list[int]:
    """The sample, drawn as baluarte.evidence.sampling draws it."""
    ordered = sorted(set(population))
    if size >= len(ordered):
        return ordered
    return sorted(random.Random(seed).sample(ordered, size))  # noqa: S311


def _jsonl(data: bytes) -> list[Any]:
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def _csv_rows(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))


def verify_package(contents: dict[str, bytes], package: dict[str, Any], report: Report) -> None:
    """The change evidence digests, and the sample redrawn from its seed."""
    for name in PACKAGE_FILES:
        if not report.check(name in contents, f"{name}: required by an evidence package"):
            return
    evidence = _jsonl(contents["change_evidence.jsonl"])
    for observation in evidence:
        ident = f"change observation {observation.get('id')}"
        if observation.get("payload_withheld"):
            report.warn(f"{ident}: payload withheld from the package, not verifiable")
            continue
        payload = observation.get("payload")
        stored = observation.get("payload_digest")
        if payload is None:
            report.check(stored is None, f"{ident}: digest without a payload")
            continue
        matches = stored == sha256(canonical(payload))
        if observation.get("digest_valid") is False:
            report.warn(f"{ident}: its payload no longer matched its digest when exported")
            report.check(not matches, f"{ident}: digest_valid false but it matches")
        else:
            report.check(matches, f"{ident}: payload does not match payload_digest")
    population = [
        int(row["id"]) for row in _csv_rows(contents["changes.csv"]) if row["kind"] in SAMPLED_KINDS
    ]
    sample = package.get("sample", {})
    report.check(
        sample.get("population") == len(population),
        "changes.csv: the population differs from the manifest's sample",
    )
    redrawn = draw(population, int(sample.get("requested_size", 0)), int(sample.get("seed", 0)))
    report.check(
        redrawn == sample.get("change_ids"),
        "change sample: redrawing with the manifest's seed gives a different sample",
    )
    listed = [int(row["change_id"]) for row in _csv_rows(contents["change_sample.csv"])]
    report.check(listed == sample.get("change_ids"), "change_sample.csv: differs from the manifest")
    evidenced = {observation.get("change_id") for observation in evidence}
    for change_id in listed:
        report.check(change_id in evidenced, f"change {change_id}: sampled but without evidence")


def verify_register(contents: dict[str, bytes], observations: list[Any], report: Report) -> None:
    """Captured files against their names, and the issue-to-control index against itself."""
    for name in REGISTER_FILES:
        if not report.check(name in contents, f"{name}: required by a register export"):
            return
    stored = {}
    for name, data in contents.items():
        if name.startswith("files/"):
            sha = name[len("files/") :]
            report.check(sha == sha256(data), f"{name}: content does not match its name")
            stored[sha] = data
    listed = {row["sha256"]: row for row in _csv_rows(contents["files.csv"])}
    for sha, row in listed.items():
        if report.check(sha in stored, f"files.csv: {sha} listed but files/{sha} missing"):
            report.check(int(row["size_bytes"]) == len(stored[sha]), f"files/{sha}: size differs")
    for sha in stored:
        report.check(sha in listed, f"files/{sha}: not listed in files.csv")
    missing = {
        row["ref"] for row in _csv_rows(contents["gaps.csv"]) if row["code"] == "file_missing"
    }
    captured = {}
    for observation in observations:
        captured[observation.get("id")] = observation
        payload = observation.get("payload") or {}
        for attachment in payload.get("attachments") or []:
            cited = attachment.get("sha256") if isinstance(attachment, dict) else None
            if cited:
                report.check(
                    cited in stored or cited in missing,
                    f"observation {observation.get('id')}: attachment {cited} neither in the "
                    "package nor listed as missing",
                )
    issues = {(row["project_id"], row["iid"]) for row in _csv_rows(contents["issues.csv"])}
    mappings = {}
    for row in _csv_rows(contents["issue_controls.csv"]):
        mappings[row["mapping_id"]] = row
        ident = f"mapping {row['mapping_id']}"
        report.check((row["project_id"], row["iid"]) in issues, f"{ident}: its issue is missing")
        report.check(
            int(row["observation_id"]) in captured, f"{ident}: the capture it cites is missing"
        )
    for row in _csv_rows(contents["mapping_evidence.csv"]):
        report.check(
            row["mapping_id"] in mappings,
            f"mapping_evidence.csv: item of mapping {row['mapping_id']} not in the index",
        )


def verify_release(contents: dict[str, bytes], observations: list[Any], report: Report) -> None:
    """Reports against their names and listing, and every cited observation present."""
    for name in RELEASE_FILES:
        if not report.check(name in contents, f"{name}: required by an audit release"):
            return
    stored = {}
    for name, data in contents.items():
        if name.startswith("reports/"):
            sha = name[len("reports/") :]
            report.check(sha == sha256(data), f"{name}: content does not match its name")
            stored[sha] = data
    fetched = {}
    for row in _csv_rows(contents["reports.csv"]):
        if row["status"] == "fetched":
            fetched[row["sha256"]] = row
            if report.check(
                row["sha256"] in stored,
                f"reports.csv: {row['sha256']} fetched but reports/{row['sha256']} missing",
            ):
                report.check(
                    int(row["size_bytes"]) == len(stored[row["sha256"]]),
                    f"reports/{row['sha256']}: size differs from reports.csv",
                )
    for sha in stored:
        report.check(sha in fetched, f"reports/{sha}: not listed as fetched in reports.csv")
    present = {observation.get("id") for observation in observations}
    release = json.loads(contents["release.json"])
    cited = [release.get("deployment", {}).get("observation_id")]
    protections = release.get("protections")
    if isinstance(protections, dict):
        cited.append(protections.get("observation_id"))
    cited += [int(row["observation_id"]) for row in _csv_rows(contents["merge_requests.csv"])]
    for ident in cited:
        report.check(ident in present, f"observation {ident}: cited but not in the package")


def verify_json(document: dict[str, Any], report: Report) -> tuple[dict[str, Any], list[Any]]:
    manifest = document.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("the document has no manifest: this is not a complete Baluarte export")
    metadata = {key: document[key] for key in METADATA if key in document}
    report.check(
        manifest.get("metadata_sha256") == sha256(canonical(metadata)),
        "metadata: does not match the manifest",
    )
    sections = {entry["name"]: entry for entry in manifest.get("sections", [])}
    for name, entry in sections.items():
        rows = document.get(name)
        if not isinstance(rows, list):
            report.check(False, f"{name}: section missing")
            continue
        digest = hashlib.sha256()
        for row in rows:
            digest.update(canonical(row) + b"\n")
        report.check(entry["sha256"] == digest.hexdigest(), f"{name}: does not match the manifest")
        report.check(entry["rows"] == len(rows), f"{name}: row count differs from the manifest")
    known = set(METADATA) | set(sections) | {"counts", "manifest"}
    for name in document:
        report.check(name in known, f"{name}: not listed in the manifest")
    report.check(
        document.get("counts") == manifest.get("counts"), "counts: differ from the manifest"
    )
    observations = document.get("observations")
    return manifest, observations if isinstance(observations, list) else []


def verify_observations(
    observations: Iterable[Any], manifest: dict[str, Any], report: Report
) -> None:
    invalid = 0
    for observation in observations:
        ident = observation.get("id")
        payload = observation.get("payload")
        stored = observation.get("payload_digest")
        if observation.get("digest_valid") is False:
            invalid += 1
            report.warn(
                f"observation {ident}: its payload no longer matched its digest when exported "
                "(digest_valid false)"
            )
        if observation.get("payload_withheld"):
            report.warn(f"observation {ident}: payload withheld from the export, not verifiable")
            continue
        if payload is None:
            report.check(stored is None, f"observation {ident}: digest without a payload")
            continue
        matches = stored == sha256(canonical(payload))
        if observation.get("digest_valid") is False:
            # Exported as stored: a match now means the payload or flag was edited afterwards.
            report.check(not matches, f"observation {ident}: digest_valid false but it matches")
        else:
            report.check(matches, f"observation {ident}: payload does not match payload_digest")
    summary = manifest.get("observations", {})
    report.check(
        summary.get("digest_invalid") == invalid,
        "observations: invalid digest count differs from the manifest",
    )


def verify_expected(manifest: dict[str, Any], report: Report) -> None:
    """Contents the manifest's export format promises, and the exceptions summary."""
    package = manifest.get("package")
    listed = {
        entry["name"]: entry
        for entry in manifest.get("files" if package == "zip" else "sections", [])
    }
    required = REQUIRED.get(str(manifest.get("export_format")), {}).get(str(package))
    if required is not None:
        report.check(required in listed, f"{required}: required by the export format but missing")
    summary = manifest.get("exceptions")
    if summary is not None and required in listed:
        report.check(
            summary.get("count") == listed[required]["rows"],
            "exceptions: count differs from the manifest's rows",
        )


def verify(path: str, manifest_sha256: str | None = None) -> tuple[Report, dict[str, Any]]:
    report = Report()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            manifest, observations = verify_zip(archive, report)
    else:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, dict):
            raise ValueError("not a Baluarte export")
        manifest, observations = verify_json(document, report)
    verify_observations(observations, manifest, report)
    verify_expected(manifest, report)
    if manifest_sha256 is not None:
        report.check(
            sha256(canonical(manifest)) == manifest_sha256.strip().lower(),
            "manifest: SHA-256 differs from the audit event",
        )
    return report, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="baluarte-verify-export", description="Check a Baluarte evidence export offline."
    )
    parser.add_argument("file", help="the exported .zip or .json file")
    parser.add_argument(
        "--manifest-sha256",
        help="manifest_sha256 from the export's completion audit event",
    )
    args = parser.parse_args(argv)
    try:
        report, manifest = verify(args.file, args.manifest_sha256)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"ERROR: cannot verify {args.file}: {error}", file=sys.stderr)
        return 2
    print(f"export {manifest.get('export_id')} generated {manifest.get('generated_at')}")
    print(f"manifest sha256 {sha256(canonical(manifest))}")
    covered = manifest.get("population")
    if isinstance(covered, dict) and isinstance(covered.get("statement"), str):
        print(f"population {covered.get('basis')}: {covered['statement']}")
    for warning in report.warnings:
        print(f"WARN {warning}")
    for failure in report.failures:
        print(f"FAIL {failure}")
    if args.manifest_sha256 is None:
        print(
            "NOTE the manifest itself was not checked: pass --manifest-sha256 with the value from "
            "the export's completion audit event (evidence.export_completed, or "
            "register.export_completed for an issue register export, release.export_completed "
            "for an audit release)"
        )
    verdict = "FAILED" if report.failures else "OK"
    print(f"{verdict}: {report.passed} checks passed, {len(report.failures)} failed")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
