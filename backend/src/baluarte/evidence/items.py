"""Evidence items (roadmap M15.2, docs/EVIDENCE_MANAGEMENT.md#evidence-items-m152).

Evidence a person adds: an uploaded file or a reference to a document elsewhere, with the
controls it supports (in a framework version), the period it covers and how long it stays fresh.

- **Lifecycle.** ``draft`` (editable and deletable by whoever added it) -> ``submitted`` ->
  ``accepted`` or ``rejected`` by someone with ``evidence:review`` other than whoever added it;
  development mode lets them review their own, recorded as ``development_review``. A decided item
  never changes; a correction is a new item that supersedes an accepted one. Triggers enforce
  the same (migration 0051).
- **Files** are stored once by SHA-256 in ``captured_file``, at most 25 MiB, and only when their
  content is one of the allowed types (``sniff``): PDF, PNG, JPEG, plain text, CSV, JSON or an
  Office Open XML document. They are always served as downloads.
- **References** are checked only against hosts the deployment allows
  (``BALUARTE_EVIDENCE_CHECK_HOSTS``) and only over HTTPS; one that cannot be reached is
  ``unreachable``, never assumed fine.
- **Freshness** is ``fresh`` until ``valid_until``, then ``stale``; ``unknown`` without one.
- **Restricted** items are visible only to reviewers and whoever added them.
"""

import hashlib
import json
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from typing import Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import ColumnElement, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased

from baluarte.audit import record
from baluarte.controls import versions
from baluarte.db.models import (
    EVIDENCE_TITLE,
    MAX_EVIDENCE_DESCRIPTION,
    MAX_EVIDENCE_FILE_BYTES,
    MAX_EVIDENCE_URL,
    AssessmentCitation,
    CapturedFile,
    ControlAssessment,
    EvidenceItem,
    EvidenceItemControl,
    FrameworkControl,
    FrameworkVersion,
)
from baluarte.settings import development_mode, env
from baluarte.workspaces import current_workspace_id

CHECK_HOSTS = "BALUARTE_EVIDENCE_CHECK_HOSTS"
CHECK_TIMEOUT_SECONDS = 10.0
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")

Freshness = Literal["fresh", "stale", "unknown"]


class EvidenceRefused(Exception):
    """The request cannot be done as asked; ``code`` says why (409, or 422 for bad input)."""

    def __init__(self, code: str, message: str, *, invalid: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.invalid = invalid


class NotAllowed(Exception):
    """The caller may not do this to this item (403)."""


@dataclass(frozen=True)
class ControlRef:
    framework: str
    control_id: str
    # The framework version; the current one when omitted.
    version: str | None = None


@dataclass(frozen=True)
class Actor:
    user_id: int
    actor: str


@dataclass(frozen=True)
class Fields:
    title: str
    description: str
    owner_user_id: int | None
    period_start: date | None
    period_end: date | None
    valid_until: date | None
    restricted: bool
    controls: Sequence[ControlRef]
    url: str | None = None


# --- files ---------------------------------------------------------------------------------

OFFICE = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
OFFICE_PART = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}


def safe_filename(name: str) -> str:
    cleaned = SAFE_NAME.sub("_", name.replace("\\", "/").rsplit("/", 1)[-1]).strip("._ ")
    return cleaned[:120]


def sniff(content: bytes, filename: str) -> str:
    """The content type of an allowed file, judged by its bytes (and, for Office documents and
    text, its extension); ``EvidenceRefused`` for anything else."""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"PK\x03\x04") and suffix in OFFICE:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile:
            names = []
        if "[Content_Types].xml" in names and any(n.startswith(OFFICE_PART[suffix]) for n in names):
            return OFFICE[suffix]
    if b"\x00" not in content:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        if decoded is not None:
            if suffix == ".json":
                try:
                    json.loads(decoded)
                except ValueError:
                    pass
                else:
                    return "application/json"
            elif suffix == ".csv":
                return "text/csv"
            elif suffix in (".txt", ".md", ".log", ""):
                return "text/plain"
    raise EvidenceRefused(
        "file_type",
        "Upload a PDF, PNG, JPEG, plain text, CSV, JSON, Word, Excel or PowerPoint file",
        invalid=True,
    )


# --- reading ---------------------------------------------------------------------------------


def freshness(item: EvidenceItem, today: date) -> Freshness:
    if item.valid_until is None:
        return "unknown"
    return "fresh" if item.valid_until >= today else "stale"


def visible(item: EvidenceItem, user_id: int, reviewer: bool) -> bool:
    return not item.restricted or reviewer or item.added_by_user_id == user_id


def controls_of(db: Session, item_ids: Iterable[int]) -> dict[int, list[tuple[str, str, str]]]:
    """Each item's controls as (framework, version, control_id)."""
    ids = list(item_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(
            EvidenceItemControl.evidence_item_id,
            FrameworkVersion.framework,
            FrameworkVersion.version,
            FrameworkControl.control_id,
        )
        .join(FrameworkControl, FrameworkControl.id == EvidenceItemControl.framework_control_id)
        .join(FrameworkVersion, FrameworkVersion.id == FrameworkControl.framework_version_id)
        .where(EvidenceItemControl.evidence_item_id.in_(ids))
        .order_by(EvidenceItemControl.id)
    ).all()
    out: dict[int, list[tuple[str, str, str]]] = {i: [] for i in ids}
    for item_id, framework, label, control_id in rows:
        out[item_id].append((framework, label, control_id))
    return out


def superseded_by(db: Session, item_ids: Iterable[int]) -> dict[int, int]:
    """For each item, the accepted item that supersedes it."""
    ids = list(item_ids)
    if not ids:
        return {}
    return {
        old: new
        for new, old in db.execute(
            select(EvidenceItem.id, EvidenceItem.supersedes_id).where(
                EvidenceItem.supersedes_id.in_(ids), EvidenceItem.state == "accepted"
            )
        ).all()
        if old is not None
    }


def items(
    db: Session,
    *,
    user_id: int,
    reviewer: bool,
    state: str | None = None,
    control: ControlRef | None = None,
) -> list[EvidenceItem]:
    query = select(EvidenceItem).order_by(EvidenceItem.id.desc()).limit(500)
    if state is not None:
        query = query.where(EvidenceItem.state == state)
    if control is not None:
        version_ids = [
            v.id
            for v in versions.versions(db, control.framework)
            if control.version is None or v.version == control.version
        ]
        query = query.where(
            EvidenceItem.id.in_(
                select(EvidenceItemControl.evidence_item_id)
                .join(
                    FrameworkControl,
                    FrameworkControl.id == EvidenceItemControl.framework_control_id,
                )
                .where(
                    FrameworkControl.control_id == control.control_id,
                    FrameworkControl.framework_version_id.in_(version_ids),
                )
            )
        )
    if not reviewer:
        query = query.where(
            (EvidenceItem.restricted.is_(False)) | (EvidenceItem.added_by_user_id == user_id)
        )
    return list(db.scalars(query))


# --- writing ---------------------------------------------------------------------------------


def _clean(fields: Fields, kind: str) -> Fields:
    title = fields.title.strip()
    if not EVIDENCE_TITLE[0] <= len(title) <= EVIDENCE_TITLE[1]:
        raise EvidenceRefused(
            "title",
            f"Give a title of {EVIDENCE_TITLE[0]} to {EVIDENCE_TITLE[1]} characters",
            invalid=True,
        )
    description = fields.description.strip()
    if len(description) > MAX_EVIDENCE_DESCRIPTION:
        raise EvidenceRefused("description", "The description is too long", invalid=True)
    if fields.period_start and fields.period_end and fields.period_end < fields.period_start:
        raise EvidenceRefused("period", "The period ends before it starts", invalid=True)
    url = fields.url.strip() if fields.url else None
    if kind == "reference":
        if not url or len(url) > MAX_EVIDENCE_URL or urlsplit(url).scheme not in ("http", "https"):
            raise EvidenceRefused("url", "Give an http or https address", invalid=True)
        if not urlsplit(url).hostname:
            raise EvidenceRefused("url", "Give an http or https address", invalid=True)
    elif url:
        raise EvidenceRefused("url", "A file item has no address", invalid=True)
    return Fields(
        title=title,
        description=description,
        owner_user_id=fields.owner_user_id,
        period_start=fields.period_start,
        period_end=fields.period_end,
        valid_until=fields.valid_until,
        restricted=fields.restricted,
        controls=fields.controls,
        url=url,
    )


def _control_ids(db: Session, refs: Sequence[ControlRef]) -> list[int]:
    found: list[int] = []
    for ref in refs:
        owner = (
            versions.version(db, ref.framework, ref.version)
            if ref.version is not None
            else versions.current(db, ref.framework)
        )
        row = versions.control_row(db, owner.id, ref.control_id) if owner is not None else None
        if row is None:
            raise EvidenceRefused(
                "unknown_control",
                f"{ref.framework} {ref.control_id} is not in that framework version",
                invalid=True,
            )
        if row.id not in found:
            found.append(row.id)
    return found


def _link(db: Session, item: EvidenceItem, control_ids: Sequence[int]) -> None:
    current = {
        link.framework_control_id: link
        for link in db.scalars(
            select(EvidenceItemControl).where(EvidenceItemControl.evidence_item_id == item.id)
        )
    }
    for control_id, link in current.items():
        if control_id not in control_ids:
            db.delete(link)
    for control_id in control_ids:
        if control_id not in current:
            db.add(EvidenceItemControl(evidence_item_id=item.id, framework_control_id=control_id))
    db.flush()


def _audit(db: Session, by: Actor, action: str, item: EvidenceItem, **details: object) -> None:
    record(
        db,
        actor=by.actor,
        action=f"evidence_item.{action}",
        resource_type="evidence_item",
        resource_id=str(item.id),
        details={"kind": item.kind, "state": item.state, **details},
    )


def create(
    db: Session, kind: str, fields: Fields, by: Actor, supersedes_id: int | None = None
) -> EvidenceItem:
    cleaned = _clean(fields, kind)
    control_ids = _control_ids(db, cleaned.controls)
    if supersedes_id is not None:
        old = db.get(EvidenceItem, supersedes_id)
        if old is None or old.state != "accepted":
            raise EvidenceRefused("supersedes", "Only an accepted item can be superseded")
    item = EvidenceItem(
        workspace_id=current_workspace_id(db),
        kind=kind,
        title=cleaned.title,
        description=cleaned.description,
        owner_user_id=cleaned.owner_user_id or by.user_id,
        period_start=cleaned.period_start,
        period_end=cleaned.period_end,
        valid_until=cleaned.valid_until,
        restricted=cleaned.restricted,
        url=cleaned.url,
        supersedes_id=supersedes_id,
        state="draft",
        added_by=by.actor,
        added_by_user_id=by.user_id,
    )
    db.add(item)
    db.flush()
    _link(db, item, control_ids)
    _audit(db, by, "added", item, controls=len(control_ids), supersedes_id=supersedes_id)
    return item


def _own_draft(item: EvidenceItem, by: Actor) -> None:
    if item.added_by_user_id != by.user_id:
        raise NotAllowed("Only whoever added an evidence item may change it")
    if item.state != "draft":
        raise EvidenceRefused("not_draft", "Only a draft can change; add a new item instead")


def update(db: Session, item: EvidenceItem, fields: Fields, by: Actor) -> EvidenceItem:
    _own_draft(item, by)
    cleaned = _clean(fields, item.kind)
    control_ids = _control_ids(db, cleaned.controls)
    item.title = cleaned.title
    item.description = cleaned.description
    item.owner_user_id = cleaned.owner_user_id or item.owner_user_id
    item.period_start = cleaned.period_start
    item.period_end = cleaned.period_end
    item.valid_until = cleaned.valid_until
    item.restricted = cleaned.restricted
    if item.kind == "reference":
        item.url = cleaned.url
    _link(db, item, control_ids)
    _audit(db, by, "edited", item, controls=len(control_ids))
    return item


def attach(db: Session, item: EvidenceItem, content: bytes, filename: str, by: Actor) -> None:
    """Store a draft file item's file (replacing any it had)."""
    _own_draft(item, by)
    if item.kind != "file":
        raise EvidenceRefused("not_a_file", "A reference has no file")
    if not content:
        raise EvidenceRefused("empty", "The file is empty", invalid=True)
    if len(content) > MAX_EVIDENCE_FILE_BYTES:
        raise EvidenceRefused("too_large", "A file is at most 25 MiB", invalid=True)
    name = safe_filename(filename)
    if not name:
        raise EvidenceRefused("filename", "Give the file's name", invalid=True)
    content_type = sniff(content, name)
    sha = hashlib.sha256(content).hexdigest()
    db.execute(
        pg_insert(CapturedFile)
        .values(sha256=sha, size_bytes=len(content), content_type=content_type, content=content)
        .on_conflict_do_nothing(index_elements=[CapturedFile.sha256])
    )
    item.file_sha256 = sha
    item.filename = name
    item.content_type = content_type
    item.size_bytes = len(content)
    db.flush()
    _audit(db, by, "file_attached", item, sha256=sha, size_bytes=len(content))


def delete(db: Session, item: EvidenceItem, by: Actor) -> None:
    _own_draft(item, by)
    _audit(db, by, "deleted", item, title=item.title)
    db.delete(item)
    db.flush()


def submit(db: Session, item: EvidenceItem, by: Actor) -> EvidenceItem:
    _own_draft(item, by)
    if item.kind == "file" and item.file_sha256 is None:
        raise EvidenceRefused("no_file", "Upload the file before submitting")
    if not controls_of(db, [item.id])[item.id]:
        raise EvidenceRefused("no_controls", "Link at least one control before submitting")
    item.state = "submitted"
    item.submitted_at = func.now()
    db.flush()
    _audit(db, by, "submitted", item)
    return item


def decide(
    db: Session, item: EvidenceItem, accept: bool, note: str | None, by: Actor
) -> EvidenceItem:
    if item.state != "submitted":
        raise EvidenceRefused("not_submitted", "Only a submitted item can be reviewed")
    own = item.added_by_user_id == by.user_id
    if own and not development_mode():
        raise NotAllowed("Someone other than whoever added an evidence item reviews it")
    cleaned = (note or "").strip() or None
    if not accept and cleaned is None:
        raise EvidenceRefused("note", "Say why the item is rejected", invalid=True)
    item.state = "accepted" if accept else "rejected"
    item.reviewed_by = by.actor
    item.reviewed_by_user_id = by.user_id
    item.reviewed_at = func.now()
    item.review_note = cleaned
    item.development_review = own
    db.flush()
    _audit(db, by, "accepted" if accept else "rejected", item, development_review=own)
    return item


# --- reference checks --------------------------------------------------------------------------


def allowed_hosts() -> frozenset[str]:
    return frozenset(h.strip().lower() for h in env(CHECK_HOSTS).split(",") if h.strip())


def check(
    db: Session, item: EvidenceItem, by: Actor, client: httpx.Client | None = None
) -> EvidenceItem:
    """Fetch a reference once and record whether it was reachable and what it held."""
    if item.kind != "reference" or item.url is None:
        raise EvidenceRefused("not_a_reference", "Only a reference is checked")
    parts = urlsplit(item.url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in allowed_hosts():
        raise EvidenceRefused(
            "check_not_allowed",
            f"Checks are allowed only over https to the hosts in {CHECK_HOSTS}",
        )
    status: int | None = None
    digest: str | None = None
    owned = client is None
    http = client or httpx.Client(timeout=CHECK_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        with http.stream("GET", item.url) as response:
            status = response.status_code
            if 200 <= status < 300:
                hasher = hashlib.sha256()
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_EVIDENCE_FILE_BYTES:
                        hasher = None  # type: ignore[assignment]
                        break
                    hasher.update(chunk)
                digest = hasher.hexdigest() if hasher is not None else None
    except httpx.HTTPError:
        status = None
    finally:
        if owned:
            http.close()
    reachable = status is not None and 200 <= status < 300
    item.check_status = "reachable" if reachable else "unreachable"
    item.check_http_status = status
    item.check_sha256 = digest
    item.checked_at = func.now()
    db.flush()
    _audit(db, by, "checked", item, check_status=item.check_status, http_status=status)
    return item


# --- retention ---------------------------------------------------------------------------------


def cited_in_force(
    item_id: "ColumnElement[int] | InstrumentedAttribute[int]",
) -> ColumnElement[bool]:
    """Whether an assessment still in force cites the item: a draft, a submitted one, or an
    approved one no approved assessment supersedes (roadmap M15.3)."""
    later = aliased(ControlAssessment)
    return exists().where(
        AssessmentCitation.evidence_item_id == item_id,
        ControlAssessment.id == AssessmentCitation.assessment_id,
        ControlAssessment.state != "rejected",
        ~exists().where(later.supersedes_id == ControlAssessment.id, later.state == "approved"),
    )


def expire_files(db: Session, cutoff: date) -> dict[str, int]:
    """Remove the files of reviewed items whose evidence ended before ``cutoff`` (their
    ``valid_until``, else the end of their period, else the day they were added), keeping the
    item and the file's SHA-256, unless an assessment in force cites the item (``cited_in_force``).
    A stored file is deleted only when no other item and no collected observation still cites it
    (``baluarte_expire_captured_file``, migration 0051). Called by the retention job; the caller
    commits."""
    ended = func.coalesce(
        EvidenceItem.valid_until, EvidenceItem.period_end, func.date(EvidenceItem.added_at)
    )
    due = list(
        db.scalars(
            select(EvidenceItem).where(
                EvidenceItem.state.in_(("accepted", "rejected")),
                EvidenceItem.file_sha256.is_not(None),
                ended < cutoff,
                ~cited_in_force(EvidenceItem.id),
            )
        )
    )
    shas = {item.file_sha256 for item in due if item.file_sha256 is not None}
    for item in due:
        item.removed_sha256 = item.file_sha256
        item.file_sha256 = None
        item.file_removed_at = func.now()
    db.flush()
    deleted = sum(
        1
        for sha in sorted(shas)
        if db.execute(text("SELECT baluarte_expire_captured_file(:sha)"), {"sha": sha}).scalar_one()
    )
    return {"evidence_item_file": len(due), "captured_file": deleted}
