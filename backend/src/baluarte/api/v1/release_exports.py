"""Production releases and audit release packages (RG.4, docs/RELEASE_GOVERNANCE.md).

``GET /releases`` lists recorded production deployments, newest first. ``POST /releases/exports``
builds the audit release of one (``baluarte.releases.export``), fetching the release pipeline's
reports from GitLab read-only unless ``include_reports`` is false. Building a package needs
``evidence:package`` (admin, analyst and auditor), like other evidence packages. It is audited
twice: ``release.export_created`` with the release when it is accepted, and
``release.export_completed`` with the SHA-256 of the ZIP and of its manifest once it is built,
before any byte is sent. ``GET /releases/exports/{export_id}`` reads both back.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.configuration.crypto import SecretUnavailable, load_secret_box
from baluarte.connections import ConnectionRefused, settings_for
from baluarte.db.models import AuditEvent, ChangeRecord, GitLabInstance, GitLabProject, Observation
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.releases import export, reports
from baluarte.settings import ConfigurationError

router = APIRouter(prefix="/releases", tags=["releases"])

Reader = Annotated[Principal, Depends(require(Permission.FINDINGS_READ))]
Packager = Annotated[Principal, Depends(require(Permission.EVIDENCE_PACKAGE))]
Database = Annotated[Session, Depends(database)]
RESOURCE = "release_export"
MAX_LIST = 200


class ReleaseOut(BaseModel):
    change_id: int
    connection_id: int
    project_id: int
    project_path: str
    deployment_id: int | None
    environment: str | None
    ref: str | None
    tag: bool | None
    sha: str | None
    finished_at: datetime
    deployer: str | None
    started_by: str | None
    approvers: list[str] | None
    release_name: str | None


def _approvers(payload: dict[str, Any]) -> list[str] | None:
    part = payload.get("deployment_approvals")
    if not isinstance(part, dict) or part.get("status") != "ok":
        return None
    return sorted(
        {
            a["username"]
            for a in part.get("approvals") or []
            if isinstance(a, dict)
            and a.get("status") == "approved"
            and isinstance(a.get("username"), str)
        }
    )


@router.get(
    "",
    response_model=list[ReleaseOut],
    summary="Recorded production releases, newest first",
    description=(
        "Each successful production deployment Baluarte recorded, with who started, approved "
        "and deployed it as GitLab records them. Filter by `connection_id` or `project_id`; at "
        f"most `limit` ({MAX_LIST} at most)."
    ),
)
def list_releases(
    _: Reader,
    db: Database,
    connection_id: Annotated[int | None, Query(gt=0)] = None,
    project_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
) -> list[ReleaseOut]:
    query = (
        select(ChangeRecord, GitLabProject, Observation)
        .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
        .join(Observation, Observation.id == ChangeRecord.observation_id)
        .where(ChangeRecord.kind == "deployment")
    )
    if connection_id is not None:
        query = query.where(ChangeRecord.instance_id == connection_id)
    if project_id is not None:
        query = query.where(ChangeRecord.project_id == project_id)
    rows = db.execute(
        query.order_by(ChangeRecord.changed_at.desc(), ChangeRecord.id.desc()).limit(limit)
    ).all()
    out = []
    for record, project, observation in rows:
        payload = observation.payload if isinstance(observation.payload, dict) else {}
        found_trigger = payload.get("trigger")
        trigger: dict[str, Any] = found_trigger if isinstance(found_trigger, dict) else {}
        found_release = payload.get("release")
        release: dict[str, Any] = found_release if isinstance(found_release, dict) else {}
        out.append(
            ReleaseOut(
                change_id=record.id,
                connection_id=record.instance_id,
                project_id=project.id,
                project_path=project.path_with_namespace,
                deployment_id=record.deployment_id,
                environment=payload.get("environment"),
                ref=payload.get("ref"),
                tag=payload.get("tag"),
                sha=payload.get("sha"),
                finished_at=record.changed_at,
                deployer=payload.get("deployer"),
                started_by=trigger.get("user") if trigger.get("status") == "ok" else None,
                approvers=_approvers(payload),
                release_name=release.get("name") if release.get("status") == "ok" else None,
            )
        )
    return out


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: int = Field(gt=0)
    # Fetch the release pipeline's reports from GitLab (read-only, size-capped).
    include_reports: bool = True


def _fetcher(
    db: Session, change_id: int, transport: httpx.BaseTransport | None
) -> export.Fetcher | str | None:
    """A report fetcher for the release's connection, or why there is none."""
    found = db.execute(
        select(GitLabInstance)
        .join(GitLabProject, GitLabProject.instance_id == GitLabInstance.id)
        .join(ChangeRecord, ChangeRecord.project_id == GitLabProject.id)
        .where(ChangeRecord.id == change_id)
    ).scalar_one_or_none()
    if found is None:
        return None
    if found.provider != "gitlab":
        return "reports are read from GitLab connections only"
    box = None
    try:
        box = load_secret_box()
    except SecretUnavailable:
        pass  # the primary connection needs none; another one is refused below
    try:
        settings = settings_for(db, found.id, box).settings
    except (ConnectionRefused, SecretUnavailable, ConfigurationError) as error:
        return f"the connection cannot be used ({error})"

    def fetch(project: int, pipeline: int) -> reports.Fetched:
        with GitLabClient(settings, transport=transport) as client:
            return reports.fetch(client, project, pipeline, datetime.now(UTC))

    return fetch


@router.post(
    "/exports",
    response_class=StreamingResponse,
    summary="Build the audit release of one production release",
    description=(
        "A ZIP for the production deployment `change_id`: the release, who started, approved "
        "and deployed it, the practice and judgment, the protections in force, the changes since "
        "the previous production release with approvals, pipelines and tests, the release "
        "pipeline's reports (fetched read-only from GitLab, each at most 10 MB and 50 MB in "
        "all, unless `include_reports` is false), every observation it rests on, gaps, "
        "`manifest.json` and `SHA256SUMS`. Never source code. Check it offline with "
        "`baluarte-verify-export`. 422 with `code` `release_unknown` or `not_a_release`. "
        "Requires `evidence:package` and the `X-CSRF-Token` header. Audited."
    ),
    responses={
        200: {"content": {"application/zip": {}}, "description": "The package"},
        422: {"description": "Not a production release"},
    },
)
def create_export(
    body: ExportIn,
    principal: Packager,
    db: Database,
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> StreamingResponse:
    export_id = uuid.uuid4().hex
    fetch: export.Fetcher | None = None
    unavailable: str | None = None
    if body.include_reports:
        found = _fetcher(db, body.change_id, transport)
        if isinstance(found, str):
            unavailable = found
        else:
            fetch = found
    if unavailable is not None:
        reason = unavailable

        def refuse(_project: int, _pipeline: int) -> reports.Fetched:
            return reports.Fetched([], reason)

        fetch = refuse
    try:
        built = export.build(
            db, body.change_id, export_id, principal.actor, datetime.now(UTC), fetch
        )
    except export.ExportRefused as error:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": error.code, "message": error.message},
        ) from error
    audit.record(
        db,
        actor=principal.actor,
        action="release.export_created",
        resource_type=RESOURCE,
        resource_id=export_id,
        details={
            "change_id": body.change_id,
            "include_reports": body.include_reports,
            "counts": built.counts,
        },
    )
    audit.record(
        db,
        actor=principal.actor,
        action="release.export_completed",
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
    change_id: int
    created_by: str
    created_at: datetime
    bytes: int | None
    sha256: str | None
    # SHA-256 of the manifest's canonical JSON, to compare with the package's manifest.json.
    manifest_sha256: str | None


@router.get(
    "/exports/{export_id}",
    response_model=ExportRecord,
    summary="An audit release's hashes, from the audit log",
    responses={404: {"description": "No such export in this workspace"}},
)
def read_export(
    export_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    _: Packager,
    db: Database,
) -> ExportRecord:
    events = {
        event.action: event
        for event in db.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_type == RESOURCE, AuditEvent.resource_id == export_id)
            .order_by(AuditEvent.id)
        )
    }
    created = events.get("release.export_created")
    if created is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such export")
    completed = events.get("release.export_completed")
    done = completed.details if completed else {}
    return ExportRecord(
        export_id=export_id,
        change_id=created.details["change_id"],
        created_by=created.actor,
        created_at=created.occurred_at,
        bytes=done.get("bytes"),
        sha256=done.get("sha256"),
        manifest_sha256=done.get("manifest_sha256"),
    )
