"""Helpers for baluarte-backup that are clearer in Python than in shell. Standard library only.

  backup_tools.py digests                  audit digests in a restored database
  backup_tools.py list RECORDS LOCAL REMOTE   recorded backups against the stored files

digests reads lines of ``id<TAB>details_digest<TAB>details`` (psql -At; details as jsonb text,
whose strings never hold a raw tab or newline) and prints ``<events> <mismatches> <first ids>``.
The digest is the one baluarte.audit.details_digest computes: SHA-256 of the canonical JSON
(sorted keys, separators "," and ":", UTF-8, non-ASCII kept). The application reads jsonb
through json.loads before hashing, and so does this. A backend test checks the two agree.

list reads three files: the recorded successful runs (``file|size|sha256|finished|offsite``),
the local files (``file|size``) and rclone's ``lsjson --metadata`` of the off-site prefix (or
an empty file when there is none). It prints one line per backup, newest first.
"""

import hashlib
import json
import sys
from pathlib import Path


def digest(details: str) -> str:
    canonical = json.dumps(
        json.loads(details), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digests() -> int:
    events = 0
    mismatched: list[str] = []
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        event_id, stored, details = line.split("\t", 2)
        events += 1
        if digest(details) != stored:
            mismatched.append(event_id)
    print(events, len(mismatched), ",".join(mismatched[:20]))
    return 0


def _rows(path: str) -> list[list[str]]:
    text = Path(path).read_text(encoding="utf-8") if path else ""
    return [line.split("|") for line in text.splitlines() if line]


def _state(stored: tuple[int, str | None] | None, size: int, sha: str) -> str:
    if stored is None:
        return "missing"
    stored_size, stored_sha = stored
    if stored_size != size or (stored_sha is not None and stored_sha != sha):
        return "DIFFERS"
    return "ok"


def list_backups(records: str, local: str, remote: str | None) -> int:
    local_files = {name: (int(size), None) for name, size in _rows(local)}
    remote_files: dict[str, tuple[int, str | None]] = {}
    if remote is not None:
        listing = Path(remote).read_text(encoding="utf-8").strip() or "[]"
        for item in json.loads(listing):
            sha = (item.get("Metadata") or {}).get("sha256")
            remote_files[item["Name"]] = (int(item["Size"]), sha)
    print(f"{'FILE':<36} {'FINISHED (UTC)':<20} {'SIZE':>12}  {'LOCAL':<8} OFF SITE")
    recorded = set()
    for name, size, sha, finished, _offsite in _rows(records):
        recorded.add(name)
        here = _state(local_files.get(name), int(size), sha)
        there = _state(remote_files.get(name), int(size), sha) if remote is not None else "-"
        print(f"{name:<36} {finished[:19]:<20} {int(size):>12}  {here:<8} {there}")
    unrecorded = sorted((set(local_files) | set(remote_files)) - recorded, reverse=True)
    for name in unrecorded:
        size = (local_files.get(name) or remote_files.get(name) or (0, None))[0]
        where = ", ".join(
            label for label, files in (("local", local_files), ("off site", remote_files))
            if name in files
        )
        print(f"{name:<36} {'(no record)':<20} {size:>12}  {where}")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["digests"]:
        return digests()
    if argv[:1] == ["list"] and len(argv) in (3, 4):
        return list_backups(argv[1], argv[2], argv[3] if len(argv) == 4 else None)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
