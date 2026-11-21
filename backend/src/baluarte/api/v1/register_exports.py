"""Register export packages (roadmap M12.4, docs/ISSUE_REGISTER.md#exports).

Building a package needs ``evidence:package`` (admin, analyst and auditor), like an audit-period
evidence package. It is audited twice: ``register.export_created`` with the scope when it is
accepted, and ``register.export_completed`` with the SHA-256 of the ZIP and of its manifest
once it is built, before any byte is sent. ``GET /register/exports/{export_id}`` reads both back.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import AuditEvent
from baluarte.register import export

router = APIRouter(prefix="/register", tags=["issue register"])

Packager = Annotated[Principal, Depends(require(Permission.EVIDENCE_PACKAGE))]
FrameworkId = Literal["soc2", "iso27001", "nist_800_53_moderate"]
RESOURCE = "register_export"


class IssueRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(ge=1)
    iid: int = Field(ge=1)


class ControlRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framework: FrameworkId
    control_id: str = Field(min_length=1, max_length=20)


class ExportIn(BaseModel):
    """One issue or a selection (``issues``), or the issues mapped to ``controls``; either may
    name an audit period (``period_id``) to date the cited evidence against."""

    model_config = ConfigDict(extra="forbid")

    issues: list[IssueRef] = Field(default_factory=list, max_length=export.MAX_ISSUES)
    controls: list[ControlRef] = Field(default_factory=list, max_length=export.MAX_CONTROLS)
    period_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_scope(self) -> "ExportIn":
        if bool(self.issues) == bool(self.controls):
            raise ValueError("name either issues or controls")
        return self

    def scope(self) -> export.Scope:
        return export.Scope(
            issues=tuple((i.project_id, i.iid) for i in self.issues),
            controls=tuple((c.framework, c.control_id) for c in self.controls),
            period_id=self.period_id,
        )


@router.post(
    "/exports",
    response_class=StreamingResponse,
    summary="Build an export package of auditable issues and their control mappings",
    description=(
        "A ZIP for one issue or a selection (`issues`), or every issue mapped to `controls` "
        "(in force or awaiting review); `period_id` dates each cited item against an audit "
        "period. It holds a readable summary, the issue-to-control index with framework "
        "editions, cited evidence and its rationale, decisions, each capture and attachment "
        "once, gaps, redactions, `manifest.json` and `SHA256SUMS`. Check it offline with "
        "`baluarte-verify-export`. 422 with `code` `issue_not_auditable`, `control_unknown`, "
        "`period_unknown`, `too_many_issues` or `too_large`. Requires `evidence:package` and the "
        "`X-CSRF-Token` header. Audited."
    ),
    responses={
        200: {"content": {"application/zip": {}}, "description": "The package"},
        422: {"description": "Invalid scope"},
    },
)
def create_export(
    request: ExportIn,
    principal: Packager,
    db: Annotated[Session, Depends(database)],
) -> StreamingResponse:
    export_id = uuid.uuid4().hex
    scope = request.scope()
    try:
        built = export.build(db, scope, export_id, principal.actor, datetime.now(UTC))
    except export.ExportRefused as error:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": error.code,
                "message": error.message,
                "reasons": [{"code": error.code, "message": r} for r in error.reasons],
            },
        ) from error
    audit.record(
        db,
        actor=principal.actor,
        action="register.export_created",
        resource_type=RESOURCE,
        resource_id=export_id,
        details={"scope": scope.described(), "counts": built.counts},
    )
    audit.record(
        db,
        actor=principal.actor,
        action="register.export_completed",
        resource_type=RESOURCE,
        resource_id=export_id,
        details={
            "bytes": built.size,
            "sha256": built.sha256,
            "manifest_sha256": built.manifest_sha256,
            "files": built.files,
        },
    )
    db.commit()
    return StreamingResponse(
        built.chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{built.filename}"',
            "Content-Length": str(built.size),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Baluarte-Export-Id": export_id,
        },
    )


class ExportRecord(BaseModel):
    export_id: str
    kind: str
    created_by: str
    created_at: datetime
    bytes: int | None
    sha256: str | None
    # SHA-256 of the manifest's canonical JSON, to compare with the package's manifest.json.
    manifest_sha256: str | None


@router.get(
    "/exports/{export_id}",
    response_model=ExportRecord,
    summary="A register export's hashes, from the audit log",
    responses={404: {"description": "No such export in this workspace"}},
)
def read_export(
    export_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    _: Packager,
    db: Annotated[Session, Depends(database)],
) -> ExportRecord:
    events = {
        event.action: event
        for event in db.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_type == RESOURCE, AuditEvent.resource_id == export_id)
            .order_by(AuditEvent.id)
        )
    }
    created = events.get("register.export_created")
    if created is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such export")
    completed = events.get("register.export_completed")
    done = completed.details if completed else {}
    return ExportRecord(
        export_id=export_id,
        kind=created.details["scope"]["kind"],
        created_by=created.actor,
        created_at=created.occurred_at,
        bytes=done.get("bytes"),
        sha256=done.get("sha256"),
        manifest_sha256=done.get("manifest_sha256"),
    )
