"""The manifest of an evidence export (BAL-028): what is in it, its hashes, and what it lacks.

Every export carries one. In a ZIP it is ``manifest.json``, written after every file it lists and
followed by ``SHA256SUMS`` (``sha256sum -c`` format, covering every file including the manifest).
In a JSON export it is the ``manifest`` member, last in the document.

A file cannot hold its own hash, so the manifest is anchored outside the package: its
``manifest_sha256`` (SHA-256 of its canonical JSON, the same algorithm as observation and audit
digests) is recorded in the ``evidence.export_completed`` audit event. ``baluarte-verify-export``
checks the package against the manifest and, given that value, the manifest against the audit log.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

MANIFEST_FORMAT = "baluarte.evidence-manifest/v1"
# Invalid observation IDs listed by ID; beyond this only the count is given.
MAX_LISTED = 1000

DIGESTS = {
    "algorithm": "SHA-256",
    "canonical_json": (
        "JSON with keys sorted, no insignificant whitespace, non-ASCII characters unescaped, "
        "encoded as UTF-8"
    ),
    "files": "SHA-256 of the file's bytes as stored in the ZIP",
    "sections": (
        "JSON export: SHA-256 of the concatenation, in document order, of each row's canonical "
        "JSON followed by a newline"
    ),
    "metadata": "JSON export: SHA-256 of the canonical JSON of the metadata members",
    "observations": "payload_digest is the SHA-256 of the payload's canonical JSON",
    "manifest": (
        "manifest_sha256 is the SHA-256 of this manifest's canonical JSON; it is recorded in the "
        "evidence.export_completed audit event, not in the package"
    ),
}

LIMITATIONS = [
    "unknown and unsupported results are neither compliant nor failing: Baluarte could not "
    "establish the value, or GitLab does not offer the feature.",
    "A rule evaluation is supporting evidence for an organization-defined control objective; it "
    "is not a SOC 2 certification, ISO 27001 certification, or NIST authorization.",
    "Digests show whether a row changed after it was written. They are not anchored outside the "
    "database: a database superuser could rewrite a row and its digest together, delete rows, or "
    "insert forged ones, and this export would not show it.",
    "The manifest proves the package matches what Baluarte produced only when its "
    "manifest_sha256 is compared with the evidence.export_completed audit event, ideally from a "
    "copy of the baluarte.audit log stream kept outside the database.",
    "Results are only as current as the collection and evaluation freshness recorded here.",
]

VERIFICATION = [
    "Run baluarte-verify-export <file> --manifest-sha256 <value from the "
    "evidence.export_completed audit event>.",
    "Without the tool: for a ZIP, run sha256sum -c SHA256SUMS in the extracted directory, then "
    "compare each file's hash with the manifest and the manifest's canonical SHA-256 with the "
    "audit event.",
    "Recompute each observation's payload_digest from its payload; digest_valid false means the "
    "stored evidence no longer matched its digest when it was exported.",
]


@dataclass
class Summary:
    """Tallies taken from the rows as they are exported (after redaction)."""

    outcomes: Counter[str] = field(default_factory=Counter)
    unknown_reasons: Counter[str] = field(default_factory=Counter)
    observation_statuses: Counter[str] = field(default_factory=Counter)
    observations: int = 0
    digest_invalid: list[int] = field(default_factory=list)
    digest_invalid_count: int = 0
    payload_withheld: int = 0
    without_payload: int = 0
    policies: list[dict[str, Any]] = field(default_factory=list)
    error_resources: Counter[tuple[str, str]] = field(default_factory=Counter)
    resources_with_errors: int = 0
    runs: list[dict[str, Any]] = field(default_factory=list)

    def add(self, section: str, row: dict[str, Any]) -> None:
        if section == "evaluations":
            self.outcomes[row["outcome"]] += 1
            if row["outcome"] == "unknown":
                self.unknown_reasons.update(row["unknown_reasons"])
        elif section == "observations":
            self.observations += 1
            self.observation_statuses[row["collection_status"]] += 1
            if row["digest_valid"] is False:
                self.digest_invalid_count += 1
                if len(self.digest_invalid) < MAX_LISTED:
                    self.digest_invalid.append(row["id"])
            if row["payload_withheld"]:
                self.payload_withheld += 1
            elif row["payload"] is None:
                self.without_payload += 1
        elif section == "policies":
            self.policies.append(
                {
                    key: row[key]
                    for key in ("id", "name", "version", "yaml_digest", "source_yaml_redacted")
                }
            )
        elif section == "collection_errors":
            self.error_resources[(row["resource_type"], row["state"])] += 1
            if row["error"] is not None:
                self.resources_with_errors += 1
        elif section == "collection_runs" and row["status"] in ("failed", "partial", "running"):
            self.runs.append(
                {
                    key: row[key]
                    for key in ("run_type", "id", "kind", "status", "started_at", "error")
                }
            )


def _freshness(meta: dict[str, Any]) -> dict[str, Any]:
    collection = meta["freshness"]["collection"]
    evaluation = meta["freshness"]["evaluation"]
    return {
        "collection": {
            "status": collection["status"],
            "checked_at": collection["checked_at"],
            "kinds": {
                kind: {
                    "status": value["status"],
                    "running": value["running"],
                    "last_success_at": value["last_success_at"],
                }
                for kind, value in sorted(collection["collections"].items())
            },
        },
        "evaluation": {
            key: evaluation.get(key)
            for key in ("status", "last_success_at", "running", "behind_collection")
        },
    }


def build(
    *,
    meta: dict[str, Any],
    package: str,
    baluarte_version: str,
    requested_by: str | None,
    counts: dict[str, int],
    contents: list[dict[str, Any]],
    summary: Summary,
    metadata_sha256: str | None = None,
) -> dict[str, Any]:
    """The manifest. ``contents`` lists the ZIP's files, or the JSON document's sections."""
    manifest: dict[str, Any] = {
        "manifest_format": MANIFEST_FORMAT,
        "export_format": meta["format"],
        "package": package,
        "export_id": meta["export_id"],
        "generated_at": meta["generated_at"],
        "baluarte_version": baluarte_version,
        "instance": meta["instance"],
        "scope": meta["scope"],
        # The internal user ID only: who to ask, without copying personal data into the package.
        "requested_by": requested_by,
        "digests": DIGESTS,
        "files" if package == "zip" else "sections": contents,
        "counts": counts,
        "policies": summary.policies,
        "evaluations": {
            "outcomes": dict(sorted(summary.outcomes.items())),
            "unknown_reasons": dict(sorted(summary.unknown_reasons.items())),
        },
        "observations": {
            "count": summary.observations,
            "by_collection_status": dict(sorted(summary.observation_statuses.items())),
            "digest_invalid": summary.digest_invalid_count,
            "digest_invalid_ids": summary.digest_invalid,
            "digest_invalid_ids_truncated": summary.digest_invalid_count > MAX_LISTED,
            "payload_withheld": summary.payload_withheld,
            "without_payload": summary.without_payload,
        },
        "collection_errors": {
            "resources": [
                {"resource_type": kind, "state": state, "count": count}
                for (kind, state), count in sorted(summary.error_resources.items())
            ],
            "with_error_message": summary.resources_with_errors,
        },
        "runs_not_succeeded": summary.runs,
        "freshness": _freshness(meta),
        "limitations": LIMITATIONS,
        "verification": VERIFICATION,
    }
    if metadata_sha256 is not None:
        manifest["metadata_sha256"] = metadata_sha256
    return manifest
