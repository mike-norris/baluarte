"""The ``LabelTaxonomy`` document and its reviewed versions.

The document names the labels every group and project in its scope should have, where they
should be defined, and the names they may be found under instead (aliases)::

    apiVersion: baluarte.io/v1alpha1
    kind: LabelTaxonomy
    metadata: {name: acme-labels}
    spec:
      scope: {groups: ["acme"]}
      placement: top_level_group
      strict: false
      labels:
        - name: "priority::high"
          color: "#d9534f"
          description: "Must be fixed this sprint"
          aliases: ["P1"]

It is read with the same restricted YAML loader as baselines. Names are compared exactly; a
name that differs only in case or spacing (``normalized``) is the same label misnamed, so two
labels of a taxonomy may not normalize alike, and no alias may normalize like any label.

Versions are stored in ``label_taxonomy`` and reviewed like baseline changes: while the
``governance`` setting requires review a new version waits ``proposed`` until someone other than
its proposer approves or rejects it; otherwise it is approved at once, ``self_approved``. The
taxonomy in force is the latest approved version. Every step is audited as ``labels.taxonomy_*``.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.audit import details_digest
from baluarte.db.models import MAX_TAXONOMY_JUSTIFICATION, LabelTaxonomy
from baluarte.policy.changes import Actor, ChangeConflict, NotAllowed, review_required
from baluarte.policy.loader import PolicyError, PolicyInvalid, digest, load_yaml
from baluarte.policy.schema import GROUP_PATTERN, Metadata

MAX_LABELS = 500
MAX_ALIASES = 20
MAX_NAME = 255
MAX_DESCRIPTION = 1000
MAX_NOTE = 2000
COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


def normalized(name: str) -> str:
    """The form names are compared in to find a misnamed label: case and spacing ignored."""
    return " ".join(name.split()).casefold()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


LabelName = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]


class LabelSpec(_Strict):
    name: LabelName
    color: Annotated[str, Field(pattern=COLOR_PATTERN, description="#RRGGBB")]
    description: Annotated[str, Field(max_length=MAX_DESCRIPTION)] | None = None
    aliases: Annotated[list[LabelName], Field(max_length=MAX_ALIASES)] = []

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise ValueError("a label name cannot be blank or start or end with spaces")
        return value


class TaxonomyScope(_Strict):
    groups: Annotated[
        list[Annotated[str, Field(pattern=GROUP_PATTERN, max_length=1000)]],
        Field(
            min_length=1,
            max_length=200,
            description='"*" for every group, or group full paths; a path covers its subgroups.',
        ),
    ]


class TaxonomySpec(_Strict):
    scope: TaxonomyScope
    placement: Literal["top_level_group"] = "top_level_group"
    strict: bool = False
    labels: Annotated[list[LabelSpec], Field(min_length=1, max_length=MAX_LABELS)]

    @model_validator(mode="after")
    def _distinct(self) -> "TaxonomySpec":
        names: dict[str, str] = {}
        for label in self.labels:
            key = normalized(label.name)
            if key in names:
                raise ValueError(f"label {label.name!r} repeats {names[key]!r}")
            names[key] = label.name
        seen: set[str] = set()
        for label in self.labels:
            for alias in label.aliases:
                key = normalized(alias)
                if key in names:
                    raise ValueError(f"alias {alias!r} of {label.name!r} names a taxonomy label")
                if key in seen:
                    raise ValueError(f"alias {alias!r} is used more than once")
                seen.add(key)
        return self


class LabelTaxonomyDocument(_Strict):
    apiVersion: Literal["baluarte.io/v1alpha1"]
    kind: Literal["LabelTaxonomy"]
    metadata: Metadata
    spec: TaxonomySpec


@dataclass(frozen=True)
class ParsedTaxonomy:
    document: LabelTaxonomyDocument
    digest: str

    @property
    def name(self) -> str:
        return self.document.metadata.name


def _path(location: tuple[int | str, ...]) -> str:
    path = ""
    for part in location:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    return path or "(document)"


def _message(error: dict[str, Any]) -> str:
    if error["type"] == "extra_forbidden":
        return "unknown field"
    if error["type"] == "missing":
        return "required field is missing"
    return str(error["msg"]).removeprefix("Value error, ")


def parse_taxonomy(source: str) -> ParsedTaxonomy:
    """Parse and validate ``source``; raise PolicyInvalid with every error found."""
    data = load_yaml(source)
    if not isinstance(data, dict):
        raise PolicyInvalid([PolicyError("(document)", "the document must be a mapping")])
    try:
        document = LabelTaxonomyDocument.model_validate(data)
    except ValidationError as exc:
        raise PolicyInvalid(
            [
                PolicyError(_path(tuple(error["loc"])), _message(dict(error)))
                for error in exc.errors(include_input=False, include_url=False)
            ]
        ) from None
    return ParsedTaxonomy(document=document, digest=digest(source))


def spec_of(row: LabelTaxonomy) -> TaxonomySpec:
    """The spec of a stored version; stored versions were validated when proposed."""
    return parse_taxonomy(row.source_yaml).document.spec


def _approved(instance_id: int, as_of: datetime | None) -> Any:
    statement = select(LabelTaxonomy).where(
        LabelTaxonomy.instance_id == instance_id, LabelTaxonomy.state == "approved"
    )
    if as_of is not None:
        statement = statement.where(LabelTaxonomy.decided_at <= as_of)
    return statement.order_by(LabelTaxonomy.decided_at.desc(), LabelTaxonomy.id.desc()).limit(1)


def in_force(
    db: Session, instance_id: int | None, as_of: datetime | None = None
) -> LabelTaxonomy | None:
    """The taxonomy in force now, or at ``as_of``: the latest version approved by then."""
    if instance_id is None:
        return None
    return db.scalars(_approved(instance_id, as_of)).first()


def versions(db: Session, instance_id: int | None) -> list[LabelTaxonomy]:
    """Every version, newest first."""
    if instance_id is None:
        return []
    return list(
        db.scalars(
            select(LabelTaxonomy)
            .where(LabelTaxonomy.instance_id == instance_id)
            .order_by(LabelTaxonomy.version.desc())
        )
    )


def _details(row: LabelTaxonomy, **extra: Any) -> dict[str, Any]:
    return {
        "name": row.name,
        "version": row.version,
        "yaml_digest": row.yaml_digest,
        "justification_digest": details_digest({"justification": row.justification})
        if row.justification
        else None,
        **extra,
    }


@dataclass(frozen=True)
class Proposed:
    row: LabelTaxonomy
    # False when the same document was already in force or waiting for review.
    created: bool


def propose(
    db: Session, instance_id: int, source: str, justification: str | None, by: Actor
) -> Proposed:
    """Store ``source`` as the next version, proposed or (without review) approved.

    The caller commits. Raises PolicyInvalid for an invalid document, ValueError for a
    justification that is too long, and ChangeConflict when another version is waiting.
    """
    parsed = parse_taxonomy(source)
    justification = (justification or "").strip() or None
    if justification is not None and len(justification) > MAX_TAXONOMY_JUSTIFICATION:
        raise ValueError(f"The justification is longer than {MAX_TAXONOMY_JUSTIFICATION}")
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"label_taxonomy:{instance_id}"))))
    current = in_force(db, instance_id)
    if current is not None and current.yaml_digest == parsed.digest:
        return Proposed(current, created=False)
    pending = db.scalars(
        select(LabelTaxonomy).where(
            LabelTaxonomy.instance_id == instance_id, LabelTaxonomy.state == "proposed"
        )
    ).first()
    if pending is not None:
        if pending.yaml_digest == parsed.digest:
            return Proposed(pending, created=False)
        raise ChangeConflict("Another version of the label taxonomy is already waiting for review")
    last = db.execute(
        select(func.max(LabelTaxonomy.version)).where(LabelTaxonomy.instance_id == instance_id)
    ).scalar_one()
    required = review_required(db)
    row = LabelTaxonomy(
        instance_id=instance_id,
        version=(last or 0) + 1,
        name=parsed.name,
        source_yaml=source,
        yaml_digest=parsed.digest,
        justification=justification,
        state="proposed" if required else "approved",
        review_required=required,
        self_approved=not required,
        proposed_by=by.actor,
        proposed_by_user_id=by.user_id,
    )
    if not required:
        row.decided_by = by.actor
        row.decided_by_user_id = by.user_id
        row.decided_at = func.now()
    db.add(row)
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="labels.taxonomy_proposed" if required else "labels.taxonomy_approved",
        resource_type="label_taxonomy",
        resource_id=str(row.id),
        details=_details(row, self_approved=not required),
    )
    return Proposed(row, created=True)


def _decidable(db: Session, taxonomy_id: int, by: Actor) -> LabelTaxonomy:
    row = db.get(LabelTaxonomy, taxonomy_id, with_for_update=True, populate_existing=True)
    if row is None:
        raise LookupError(taxonomy_id)
    if row.state != "proposed":
        raise ChangeConflict(f"The taxonomy version is already {row.state}")
    if row.proposed_by_user_id == by.user_id:
        raise NotAllowed(
            "A version must be approved or rejected by someone other than its proposer"
        )
    return row


def approve(db: Session, taxonomy_id: int, by: Actor, note: str | None) -> LabelTaxonomy:
    """Put a proposed version in force. The caller commits."""
    row = _decidable(db, taxonomy_id, by)
    row.state = "approved"
    row.decided_by = by.actor
    row.decided_by_user_id = by.user_id
    row.decided_at = func.now()
    row.decision_note = (note or "").strip() or None
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="labels.taxonomy_approved",
        resource_type="label_taxonomy",
        resource_id=str(row.id),
        details=_details(row, proposed_by=row.proposed_by, note=row.decision_note),
    )
    return row


def reject(db: Session, taxonomy_id: int, by: Actor, note: str) -> LabelTaxonomy:
    """Reject a proposed version; a note is required. The caller commits."""
    note = note.strip()
    if not note:
        raise ValueError("A rejection needs a note")
    row = _decidable(db, taxonomy_id, by)
    row.state = "rejected"
    row.decided_by = by.actor
    row.decided_by_user_id = by.user_id
    row.decided_at = func.now()
    row.decision_note = note
    db.flush()
    db.refresh(row)
    audit.record(
        db,
        actor=by.actor,
        action="labels.taxonomy_rejected",
        resource_type="label_taxonomy",
        resource_id=str(row.id),
        details=_details(row, proposed_by=row.proposed_by, note=note),
    )
    return row


def covers(scope: TaxonomyScope, group_path: str) -> bool:
    """Whether a group (by full path) is in the taxonomy's scope; a path covers its subgroups."""
    for entry in scope.groups:
        if entry == "*" or group_path == entry or group_path.startswith(entry + "/"):
            return True
    return False


def top_level(path: str) -> str:
    return path.split("/", 1)[0]


_HEX = re.compile(COLOR_PATTERN)


def same_color(observed: Any, expected: str) -> bool | None:
    """Colours compare case-insensitively; None when GitLab reported a colour that is not hex."""
    if not isinstance(observed, str) or not _HEX.match(observed):
        return None
    return observed.lower() == expected.lower()
