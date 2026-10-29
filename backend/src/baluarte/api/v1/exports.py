"""Scoped evidence export (BAL-027).

Exports are bulk copies of inventory and evidence, so they need ``evidence:export`` (admin and
auditor; not analyst) and are audited twice: ``evidence.exported`` when the export is accepted,
with its scope and planned row counts, and ``evidence.export_completed`` once every byte has been
produced, with the SHA-256 of the response and of its manifest (BAL-028), which is how an auditor
ties a package back to this deployment. An export that stops part way has no completion event.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database_engine, require
from baluarte.auth.roles import Permission
from baluarte.evidence.export import (
    MAX_ROWS,
    EvidenceExport,
    ExportFormat,
    ExportResult,
    ExportScope,
    ScopeError,
)
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evidence", tags=["evidence"])

MEDIA_TYPES: dict[str, str] = {"json": "application/json", "csv": "application/zip"}


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: ExportFormat
    scope: ExportScope


def _settings() -> GitLabSettings | None:
    try:
        return load_gitlab_settings()
    except ConfigurationError:
        return None


def _completed(engine: Engine, actor: str, export_id: str) -> Callable[[ExportResult], None]:
    def record(result: ExportResult) -> None:
        try:
            with Session(engine) as db:
                audit.record(
                    db,
                    actor=actor,
                    action="evidence.export_completed",
                    resource_type="evidence_export",
                    resource_id=export_id,
                    details={
                        "bytes": result.bytes,
                        "sha256": result.sha256,
                        "manifest_sha256": result.manifest_sha256,
                        "rows": result.rows,
                        "files": {
                            name: {"rows": f.rows, "bytes": f.bytes, "sha256": f.sha256}
                            for name, f in result.files.items()
                        },
                        "sections": {
                            name: {"rows": s.rows, "sha256": s.sha256}
                            for name, s in result.sections.items()
                        },
                    },
                )
                db.commit()
        except Exception:
            # The bytes are already sent; the accepted event records that the export happened.
            logger.exception("evidence export %s: could not record completion", export_id)

    return record


@router.post(
    "/exports",
    response_class=StreamingResponse,
    summary="Export scoped evidence as JSON or a ZIP of CSV files",
    description=(
        "`format` is `json` (one document) or `csv` (a ZIP of CSV files, observations as JSON "
        "lines, a README, `manifest.json` and `SHA256SUMS`). Every export ends with a manifest: "
        "the SHA-256 and row count of each file or section, the policy versions, observation "
        "digest results, collection errors and freshness; the manifest's own SHA-256 is in the "
        "`evidence.export_completed` audit event. The scope must narrow the export (`group`, "
        "`projects`, `policy`, `rule`, `outcome`, `finding_state`, `since`, `until`) or set "
        "`all: true`. "
        f"At most {MAX_ROWS} rows; a larger scope is refused with 422 before anything is "
        "sent. Everything is read from one database snapshot. Unknown and failed collection "
        "state is exported, never dropped. Requires the `X-CSRF-Token` header. Audited."
    ),
    responses={
        200: {
            "content": {"application/json": {}, "application/zip": {}},
            "description": "The export, streamed",
        },
        409: {"description": "GitLab is not configured, or nothing has been collected yet"},
        422: {"description": "Invalid scope, unknown group or project, or too many rows"},
    },
)
def export_evidence(
    request: ExportRequest,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_EXPORT))],
    engine: Annotated[Engine, Depends(database_engine)],
) -> StreamingResponse:
    settings = _settings()
    if settings is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
    export_id = uuid.uuid4().hex
    export = EvidenceExport(
        engine, settings, request.scope, request.format, export_id, requested_by=principal.actor
    )
    try:
        if export.instance_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Nothing has been collected yet")
        try:
            planned = export.plan()
        except ScopeError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        with Session(engine) as db:
            audit.record(
                db,
                actor=principal.actor,
                action="evidence.exported",
                resource_type="evidence_export",
                resource_id=export_id,
                details={
                    "format": request.format,
                    "scope": request.scope.described(),
                    "planned_rows": planned,
                    "instance": settings.url,
                },
            )
            db.commit()
    except BaseException:
        export.close()
        raise
    return StreamingResponse(
        export.stream(_completed(engine, principal.actor, export_id)),
        media_type=MEDIA_TYPES[request.format],
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename()}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Baluarte-Export-Id": export_id,
        },
    )
