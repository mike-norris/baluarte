"""Scoped evidence export (BAL-027).

Exports are bulk copies of inventory and evidence, so they need ``evidence:export`` (admin and
auditor; not analyst) and are audited twice: ``evidence.exported`` when the export is accepted,
with its scope and planned row counts, and ``evidence.export_completed`` once every byte has been
produced, with the SHA-256 of the response and of its manifest (BAL-028), which is how an auditor
ties a package back to this deployment. An export that stops part way has no completion event.

Audit-period packages (``baluarte.evidence.package``) need ``evidence:package`` (admin, analyst
and auditor) and are audited the same way, as ``evidence.package_created`` (with the seed and
the sampled change IDs) and ``evidence.package_completed``.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, database_engine, require
from baluarte.auth.roles import Permission
from baluarte.db.models import AuditEvent
from baluarte.evidence import sampling
from baluarte.evidence.export import (
    MAX_ROWS,
    EvidenceExport,
    ExportFormat,
    ExportResult,
    ExportScope,
    ScopeError,
)
from baluarte.evidence.package import EvidencePackage, PackageConflict, PackageRequest
from baluarte.integrations.gitlab.instances import instance_id_for
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.manager.scopes import people_scope
from baluarte.settings import ConfigurationError
from baluarte.timetracking.settings import per_person_enabled

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


def _completed(
    engine: Engine,
    actor: str,
    export_id: str,
    action: str = "evidence.export_completed",
    resource_type: str = "evidence_export",
) -> Callable[[ExportResult], None]:
    def record(result: ExportResult) -> None:
        try:
            with Session(engine) as db:
                audit.record(
                    db,
                    actor=actor,
                    action=action,
                    resource_type=resource_type,
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
    people = _people_projects(engine, principal, settings) if request.scope.include_people else None
    export_id = uuid.uuid4().hex
    export = EvidenceExport(
        engine,
        settings,
        request.scope,
        request.format,
        export_id,
        requested_by=principal.actor,
        people_projects=people,
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
            if request.scope.include_people:
                # Who exported time per person, and for which scope; never what it held.
                audit.record(
                    db,
                    actor=principal.actor,
                    action="time.people_exported",
                    resource_type="evidence_export",
                    resource_id=export_id,
                    details={
                        "scope": request.scope.described(),
                        "projects": "all" if people is None else "manager_scope",
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


def _people_projects(
    engine: Engine, principal: Principal, settings: GitLabSettings
) -> frozenset[int] | None:
    """The projects whose time per person this export may hold; None for every project.

    403 without ``time:read_people``, when the deployment turned it off, or without a scope.
    """

    def refused(code: str, message: str) -> HTTPException:
        return HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": code, "message": message})

    if Permission.TIME_READ_PEOPLE not in principal.permissions:
        raise refused("forbidden", "Exporting time per person needs time:read_people")
    try:
        enabled = per_person_enabled()
    except ConfigurationError as error:
        raise refused("per_person_disabled", str(error)) from error
    if not enabled:
        raise refused("per_person_disabled", "Time per person is turned off for this deployment")
    with Session(engine) as db:
        instance_id = instance_id_for(db, settings.url)
        found = people_scope(db, instance_id, principal.user_id, principal.roles)
    if found.all:
        return None
    if not found.project_ids:
        raise refused("out_of_scope", "No groups are assigned to you as a manager")
    return found.project_ids


PACKAGE = "evidence_package"


def _package_conflict(code: str, message: str, reasons: list[str] | None = None) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message, "reasons": reasons or []},
    )


@router.post(
    "/packages",
    response_class=StreamingResponse,
    summary="Build the evidence package for an audit period",
    description=(
        "A ZIP for the period `from` (inclusive) to `to` (exclusive): results as of `to` (the "
        "latest evaluation run that finished by then, findings as they stood then, the mapping "
        "and monitoring scope in force then), every change that landed in the period, a sample "
        "of `sample.size` merge requests and direct pushes with the full evidence of each, and "
        "a README on verifying it. Narrow it to a control (`framework` with `control_id`) or "
        "`groups`. The sample is reproducible: send the same `sample.seed` (returned in the "
        "`X-Baluarte-Sample-Seed` header and the manifest) to draw it again. 409 with `code` "
        "`not_configured`, `nothing_collected`, `period_not_over` or `no_evaluation_run`. "
        "Requires `evidence:package` and the `X-CSRF-Token` header. Audited."
    ),
    responses={
        200: {"content": {"application/zip": {}}, "description": "The package, streamed"},
        409: {"description": "The package cannot be built for this period"},
        422: {"description": "Invalid request, unknown group or control, or too many rows"},
    },
)
def create_package(
    request: PackageRequest,
    principal: Annotated[Principal, Depends(require(Permission.EVIDENCE_PACKAGE))],
    engine: Annotated[Engine, Depends(database_engine)],
) -> StreamingResponse:
    settings = _settings()
    if settings is None:
        raise _package_conflict("not_configured", "GitLab is not configured")
    export_id = uuid.uuid4().hex
    seed = request.sample.seed if request.sample.seed is not None else sampling.new_seed()
    package = EvidencePackage(
        engine, settings, request, export_id, seed, requested_by=principal.actor
    )
    try:
        if package.instance_id is None:
            raise _package_conflict("nothing_collected", "Nothing has been collected yet")
        try:
            planned = package.plan()
        except PackageConflict as error:
            raise _package_conflict(error.code, error.message, error.reasons) from error
        except ScopeError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        period = request.model_dump(mode="json", by_alias=True)
        with Session(engine) as db:
            audit.record(
                db,
                actor=principal.actor,
                action="evidence.package_created",
                resource_type=PACKAGE,
                resource_id=export_id,
                details={
                    **{key: period[key] for key in ("from", "to")},
                    "scope": request.described(),
                    "sample": {
                        "requested_size": request.sample.size,
                        "size": len(package.sampled),
                        "seed": seed,
                        "population": len(package.population),
                        "change_ids": package.sampled,
                    },
                    "evaluation_run_id": package.run.id if package.run else None,
                    "planned_rows": planned,
                    "instance": settings.url,
                },
            )
            db.commit()
    except BaseException:
        package.close()
        raise
    return StreamingResponse(
        package.stream(
            _completed(
                engine,
                principal.actor,
                export_id,
                action="evidence.package_completed",
                resource_type=PACKAGE,
            )
        ),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{package.filename()}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Baluarte-Export-Id": export_id,
            "X-Baluarte-Sample-Seed": str(seed),
        },
    )


class PackageOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    export_id: str
    # The period, as requested.
    period_from: datetime = Field(alias="from")
    period_to: datetime = Field(alias="to")
    seed: int
    sample_size: int
    # SHA-256 of the manifest's canonical JSON; null until every byte has been produced.
    manifest_sha256: str | None
    created_by: str
    created_at: datetime
    completed_at: datetime | None


@router.get(
    "/packages/{export_id}",
    response_model=PackageOut,
    response_model_by_alias=True,
    summary="An evidence package's seed and manifest hash, from the audit log",
    description="What was recorded when the package was built: its period, the seed and sample "
    "size (send the seed again to redraw the same sample), and once complete the SHA-256 of "
    "its manifest, to compare with the package's `manifest.json`.",
    responses={404: {"description": "No such package"}},
)
def read_package(
    export_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    _: Annotated[Principal, Depends(require(Permission.EVIDENCE_PACKAGE))],
    db: Annotated[Session, Depends(database)],
) -> PackageOut:
    events = {
        event.action: event
        for event in db.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_type == PACKAGE, AuditEvent.resource_id == export_id)
            .order_by(AuditEvent.id)
        )
    }
    created = events.get("evidence.package_created")
    if created is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such package")
    completed = events.get("evidence.package_completed")
    details = created.details
    return PackageOut.model_validate(
        {
            "export_id": export_id,
            "from": details["from"],
            "to": details["to"],
            "seed": details["sample"]["seed"],
            "sample_size": details["sample"]["size"],
            "manifest_sha256": completed.details.get("manifest_sha256") if completed else None,
            "created_by": created.actor,
            "created_at": created.occurred_at,
            "completed_at": completed.occurred_at if completed else None,
        }
    )
