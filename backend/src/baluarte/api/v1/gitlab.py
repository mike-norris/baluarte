"""GitLab connection diagnostics and capability detection. Nothing is ever written to GitLab."""

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import CollectionRun, GitLabGroup, GitLabInstance, GitLabProject
from baluarte.evidence.as_of import RunRef, run_at, run_ref
from baluarte.integrations.gitlab.capabilities import CapabilitySnapshot
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.diagnostics import (
    DiagnosticsReport,
    not_configured,
    run_diagnostics,
)
from baluarte.integrations.gitlab.freshness import (
    FreshnessReport,
    KindFreshness,
    RunOut,
    kind_freshness,
    report,
    stale_after,
)
from baluarte.integrations.gitlab.instances import instance_id_for
from baluarte.integrations.gitlab.jobs import (
    DETECT_CAPABILITIES,
    DISCOVER_GROUPS,
    DISCOVER_PROJECTS,
    DISCOVER_SECURITY_POLICIES,
)
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.jobs import queue
from baluarte.policy.assignments import Gap, group_ancestry
from baluarte.policy.evaluation.runner import not_evaluated
from baluarte.scope.exclusions import Coverage, Covering, coverage
from baluarte.settings import ConfigurationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gitlab", tags=["gitlab"])


def gitlab_transport() -> httpx.BaseTransport | None:
    """The HTTP transport for GitLab calls; None uses the network. Overridden in tests."""
    return None


@router.post(
    "/diagnostics",
    response_model=DiagnosticsReport,
    summary="Test the GitLab connection and the token's permissions",
    description="Calls GitLab with the configured token and reports each check as ok, warning, "
    "failed or unknown. Always 200 when the checks ran; read `status`. Requires the "
    "`X-CSRF-Token` header.",
)
def diagnose(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
    transport: Annotated[httpx.BaseTransport | None, Depends(gitlab_transport)],
) -> DiagnosticsReport:
    try:
        settings = load_gitlab_settings()
    except ConfigurationError as exc:
        report = not_configured(str(exc))
    else:
        if settings is None:
            report = not_configured("BALUARTE_GITLAB_URL is not set")
        else:
            with GitLabClient(settings, transport=transport) as client:
                report = run_diagnostics(client, settings.url)
    audit.record(
        db,
        actor=principal.actor,
        action="gitlab.diagnostics_run",
        resource_type="gitlab_instance",
        resource_id=report.gitlab_url,
        details={
            "status": report.status,
            "version": report.version,
            "checks": {check.id: check.status for check in report.checks},
        },
    )
    db.commit()
    logger.info("gitlab diagnostics: %s", report.status)
    return report


class InstanceOut(BaseModel):
    configured: bool
    url: str | None
    version: str | None
    edition: str | None
    last_checked_at: datetime | None
    stale: bool
    capabilities: CapabilitySnapshot | None


def _configured() -> GitLabSettings | None:
    try:
        return load_gitlab_settings()
    except ConfigurationError:
        return None


def _configured_url() -> str | None:
    settings = _configured()
    return settings.url if settings else None


@router.get(
    "/instance",
    response_model=InstanceOut,
    summary="The GitLab instance: version, edition and detected capabilities",
    description="`capabilities` is null until detection has run. `stale` is true when detection "
    "never ran or is older than the freshness threshold (twice the sync interval, or 24 hours "
    "when scheduling is off).",
)
def read_instance(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
) -> InstanceOut:
    settings = _configured()
    url = settings.url if settings else None
    instance = (
        db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == url)).first()
        if url
        else None
    )
    if instance is None:
        return InstanceOut(
            configured=url is not None,
            url=url,
            version=None,
            edition=None,
            last_checked_at=None,
            stale=True,
            capabilities=None,
        )
    checked = instance.last_checked_at
    return InstanceOut(
        configured=True,
        url=url,
        version=instance.version,
        edition=instance.edition,
        last_checked_at=checked,
        stale=checked is None
        or settings is None
        or datetime.now(UTC) - checked > stale_after(settings),
        capabilities=(
            CapabilitySnapshot.model_validate(instance.capability_snapshot)
            if instance.capability_snapshot
            else None
        ),
    )


class JobQueued(BaseModel):
    job_id: int


def _queue(db: Session, principal: Principal, kind: str, action: str, attempts: int) -> JobQueued:
    """Queue a GitLab job once per minute and audit the request."""
    url = _configured_url()
    if url is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitLab is not configured")
    minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    job_id = queue.enqueue(db, kind, f"{kind}:{minute}", max_attempts=attempts)
    audit.record(
        db,
        actor=principal.actor,
        action=action,
        resource_type="gitlab_instance",
        resource_id=url,
        details={"job_id": job_id},
    )
    db.commit()
    return JobQueued(job_id=job_id)


_QUEUED_DOC = (
    "Runs in the worker. Requests within the same minute share one job. Requires the "
    "`X-CSRF-Token` header."
)


@router.post(
    "/capabilities/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobQueued,
    summary="Queue capability detection",
    description=_QUEUED_DOC,
    responses={409: {"description": "GitLab is not configured"}},
)
def refresh_capabilities(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> JobQueued:
    return _queue(db, principal, DETECT_CAPABILITIES, "gitlab.capabilities_refresh_requested", 3)


@router.post(
    "/groups/discover",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobQueued,
    summary="Queue group and subgroup discovery",
    description=_QUEUED_DOC,
    responses={409: {"description": "GitLab is not configured"}},
)
def discover_groups(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> JobQueued:
    return _queue(db, principal, DISCOVER_GROUPS, "gitlab.group_discovery_requested", 5)


@router.post(
    "/projects/discover",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobQueued,
    summary="Queue project discovery",
    description=_QUEUED_DOC,
    responses={409: {"description": "GitLab is not configured"}},
)
def discover_projects(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> JobQueued:
    return _queue(db, principal, DISCOVER_PROJECTS, "gitlab.project_discovery_requested", 5)


@router.post(
    "/security-policies/discover",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobQueued,
    summary="Queue GitLab security policy discovery",
    description="Reads the security policies reaching every active group and project with "
    "GraphQL queries sent as GET. Also queued after each project discovery when capability "
    "detection found security policies readable; a run when they are not readable fails "
    "with that reason. " + _QUEUED_DOC,
    responses={409: {"description": "GitLab is not configured"}},
)
def discover_security_policies(
    principal: Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))],
    db: Annotated[Session, Depends(database)],
) -> JobQueued:
    return _queue(
        db, principal, DISCOVER_SECURITY_POLICIES, "gitlab.security_policy_discovery_requested", 3
    )


InventoryState = Literal["active", "missing", "inaccessible"]
_STATE_DOC = (
    "`state` is `active`, `inaccessible` (exists, but the token may not read it) or `missing` "
    "(deleted or hidden from the token). `last_seen_at` is when discovery last listed it."
)


MonitoringFilter = Literal["included", "excluded", "all"]
_MONITORING_DOC = (
    " `monitoring` says whether the item is out of monitoring scope and which exclusion covers "
    "it (`via`: the item itself (`project` or `group`), an excluded parent `group`, or "
    "`personal_namespaces`; `path` is the excluded project or group). Filter with `monitoring`: "
    "`included` (the default) leaves excluded items out and counts them in `excluded_hidden`; "
    "`excluded` or `all` list them."
)


class Monitoring(BaseModel):
    excluded: bool
    exclusion_id: int | None
    via: Literal["project", "group", "personal_namespaces"] | None
    path: str | None


def monitoring_of(covering: Covering | None) -> Monitoring:
    if covering is None:
        return Monitoring(excluded=False, exclusion_id=None, via=None, path=None)
    return Monitoring(
        excluded=True, exclusion_id=covering.exclusion_id, via=covering.via, path=covering.path
    )


def _monitoring_condition(
    monitoring: MonitoringFilter, excluded: ColumnElement[bool]
) -> ColumnElement[bool] | None:
    if monitoring == "included":
        return ~excluded
    if monitoring == "excluded":
        return excluded
    return None


def _scope(db: Session, url: str) -> Coverage:
    return coverage(db, instance_id_for(db, url))


class GroupOut(BaseModel):
    id: int
    gitlab_id: int
    parent_gitlab_id: int | None
    full_path: str
    name: str
    visibility: str | None
    state: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_error: str | None
    last_error_at: datetime | None
    monitoring: Monitoring


class GroupPage(BaseModel):
    items: list[GroupOut]
    next_after_id: int | None
    # Excluded groups that match the other filters but were left out (monitoring=included).
    excluded_hidden: int


def _group_out(group: GitLabGroup, scope: Coverage) -> GroupOut:
    return GroupOut.model_validate(
        {
            **{key: getattr(group, key) for key in GroupOut.model_fields if key != "monitoring"},
            "monitoring": monitoring_of(scope.for_group(group.gitlab_id)),
        }
    )


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get(
    "/groups",
    response_model=GroupPage,
    summary="Discovered groups and subgroups",
    description="Groups of the configured instance in ID order. " + _STATE_DOC + _MONITORING_DOC,
)
def list_groups(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    state: Annotated[InventoryState | None, Query()] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    parent_gitlab_id: Annotated[int | None, Query(gt=0)] = None,
    has_error: Annotated[bool | None, Query()] = None,
    monitoring: Annotated[MonitoringFilter, Query()] = "included",
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> GroupPage:
    url = _configured_url()
    if url is None:
        return GroupPage(items=[], next_after_id=None, excluded_hidden=0)
    scope = _scope(db, url)
    statement = (
        select(GitLabGroup)
        .join(GitLabInstance, GitLabInstance.id == GitLabGroup.instance_id)
        .where(GitLabInstance.base_url == url)
    )
    if state is not None:
        statement = statement.where(GitLabGroup.state == state)
    if search is not None:
        statement = statement.where(GitLabGroup.full_path.ilike(_like(search), escape="\\"))
    if parent_gitlab_id is not None:
        statement = statement.where(GitLabGroup.parent_gitlab_id == parent_gitlab_id)
    if has_error is not None:
        statement = statement.where(
            GitLabGroup.last_error.is_not(None) if has_error else GitLabGroup.last_error.is_(None)
        )
    hidden = 0
    if monitoring == "included":
        hidden = db.execute(
            select(func.count()).select_from(statement.where(scope.group_excluded()).subquery())
        ).scalar_one()
    condition = _monitoring_condition(monitoring, scope.group_excluded())
    if condition is not None:
        statement = statement.where(condition)
    if after_id is not None:
        statement = statement.where(GitLabGroup.id > after_id)
    rows = db.scalars(statement.order_by(GitLabGroup.id).limit(limit + 1)).all()
    items = [_group_out(row, scope) for row in rows[:limit]]
    return GroupPage(
        items=items,
        next_after_id=items[-1].id if len(rows) > limit else None,
        excluded_hidden=hidden,
    )


class ProjectOut(BaseModel):
    id: int
    gitlab_id: int
    namespace_gitlab_id: int
    namespace_kind: str | None
    path_with_namespace: str
    name: str
    visibility: str | None
    archived: bool
    default_branch: str | None
    state: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_error: str | None
    last_error_at: datetime | None
    monitoring: Monitoring


class ProjectPage(BaseModel):
    items: list[ProjectOut]
    next_after_id: int | None
    # Excluded projects that match the other filters but were left out (monitoring=included).
    excluded_hidden: int


def _project_out(project: GitLabProject, scope: Coverage) -> ProjectOut:
    return ProjectOut.model_validate(
        {
            **{
                key: getattr(project, key) for key in ProjectOut.model_fields if key != "monitoring"
            },
            "monitoring": monitoring_of(scope.of(project)),
        }
    )


@router.get(
    "/projects",
    response_model=ProjectPage,
    summary="Discovered projects",
    description="Projects of the configured instance in ID order, archived ones included. "
    "`visibility` and `default_branch` are null when unknown or (for the branch) when the "
    "repository is empty. `namespace_kind` is `group`, `user` (a personal project) or null "
    "when not yet known. " + _STATE_DOC + _MONITORING_DOC,
)
def list_projects(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    state: Annotated[InventoryState | None, Query()] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    namespace_gitlab_id: Annotated[int | None, Query(gt=0)] = None,
    namespace_kind: Annotated[Literal["group", "user"] | None, Query()] = None,
    visibility: Annotated[Literal["private", "internal", "public"] | None, Query()] = None,
    archived: Annotated[bool | None, Query()] = None,
    has_error: Annotated[bool | None, Query()] = None,
    monitoring: Annotated[MonitoringFilter, Query()] = "included",
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ProjectPage:
    url = _configured_url()
    if url is None:
        return ProjectPage(items=[], next_after_id=None, excluded_hidden=0)
    scope = _scope(db, url)
    statement = (
        select(GitLabProject)
        .join(GitLabInstance, GitLabInstance.id == GitLabProject.instance_id)
        .where(GitLabInstance.base_url == url)
    )
    if state is not None:
        statement = statement.where(GitLabProject.state == state)
    if search is not None:
        statement = statement.where(
            GitLabProject.path_with_namespace.ilike(_like(search), escape="\\")
        )
    if namespace_gitlab_id is not None:
        statement = statement.where(GitLabProject.namespace_gitlab_id == namespace_gitlab_id)
    if namespace_kind is not None:
        statement = statement.where(GitLabProject.namespace_kind == namespace_kind)
    if visibility is not None:
        statement = statement.where(GitLabProject.visibility == visibility)
    if archived is not None:
        statement = statement.where(GitLabProject.archived.is_(archived))
    if has_error is not None:
        statement = statement.where(
            GitLabProject.last_error.is_not(None)
            if has_error
            else GitLabProject.last_error.is_(None)
        )
    hidden = 0
    if monitoring == "included":
        hidden = db.execute(
            select(func.count()).select_from(statement.where(scope.project_excluded()).subquery())
        ).scalar_one()
    condition = _monitoring_condition(monitoring, scope.project_excluded())
    if condition is not None:
        statement = statement.where(condition)
    if after_id is not None:
        statement = statement.where(GitLabProject.id > after_id)
    rows = db.scalars(statement.order_by(GitLabProject.id).limit(limit + 1)).all()
    items = [_project_out(row, scope) for row in rows[:limit]]
    return ProjectPage(
        items=items,
        next_after_id=items[-1].id if len(rows) > limit else None,
        excluded_hidden=hidden,
    )


class Ancestor(BaseModel):
    gitlab_id: int
    full_path: str
    state: str | None


class AncestryOut(BaseModel):
    # Nearest first, up to the top-level group, as the inventory records them.
    groups: list[Ancestor]
    status: Literal["complete", "incomplete"]
    gaps: list[Gap]


class GroupDetail(BaseModel):
    group: GroupOut
    # The group's parents; empty for a top-level group.
    ancestry: AncestryOut
    subgroups: int
    projects: int
    collection: KindFreshness


class ProjectDetail(BaseModel):
    project: ProjectOut
    # The namespace group and its parents; empty for a personal project.
    ancestry: AncestryOut
    collection: KindFreshness
    # Why baselines are not evaluated for this project: "archived", "excluded" (out of
    # monitoring scope), or null when they are.
    not_evaluated: Literal["archived", "excluded"] | None
    # With ``as_of``: the moment asked for, the evaluation run in force then, and
    # ``monitoring`` as it stood then. The inventory fields are today's.
    as_of: datetime | None = None
    evaluation_run: RunRef | None = None


def _ancestry_out(db: Session, instance_id: int, start: int | None, seen: set[int]) -> AncestryOut:
    groups, gaps = group_ancestry(db, instance_id, start, seen)
    return AncestryOut(
        groups=[
            Ancestor(gitlab_id=group.target_id, full_path=group.path, state=group.state)
            for group in groups
        ],
        status="incomplete" if gaps else "complete",
        gaps=gaps,
    )


def _instance_row(db: Session) -> tuple[GitLabSettings, GitLabInstance] | None:
    settings = _configured()
    if settings is None:
        return None
    instance = db.scalars(
        select(GitLabInstance).where(GitLabInstance.base_url == settings.url)
    ).first()
    return (settings, instance) if instance is not None else None


_DETAIL_DOC = (
    "Keyed by GitLab ID, like the effective-policies and evaluations reads. `collection` is the "
    "freshness of the discovery that records it: read the item with it, since a `state` from a "
    "failed or stale collection is not current. "
)


@router.get(
    "/groups/{gitlab_id}",
    response_model=GroupDetail,
    summary="One discovered group, its parents and collection freshness",
    description=_DETAIL_DOC
    + "`subgroups` and `projects` count its direct children in the inventory. "
    + _STATE_DOC
    + " `monitoring` says whether it is out of monitoring scope.",
    responses={404: {"description": "The group is not in the inventory"}},
)
def read_group(
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
) -> GroupDetail:
    configured = _instance_row(db)
    group = (
        db.scalars(
            select(GitLabGroup).where(
                GitLabGroup.instance_id == configured[1].id, GitLabGroup.gitlab_id == gitlab_id
            )
        ).first()
        if configured
        else None
    )
    if configured is None or group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such group in the inventory")
    settings, instance = configured
    subgroups = db.execute(
        select(func.count()).where(
            GitLabGroup.instance_id == instance.id, GitLabGroup.parent_gitlab_id == gitlab_id
        )
    ).scalar_one()
    projects = db.execute(
        select(func.count()).where(
            GitLabProject.instance_id == instance.id,
            GitLabProject.namespace_gitlab_id == gitlab_id,
            GitLabProject.namespace_kind.is_distinct_from("user"),
        )
    ).scalar_one()
    return GroupDetail(
        group=_group_out(group, coverage(db, instance.id)),
        ancestry=_ancestry_out(db, instance.id, group.parent_gitlab_id, {gitlab_id}),
        subgroups=subgroups,
        projects=projects,
        collection=kind_freshness(db, instance.id, "groups", settings),
    )


@router.get(
    "/projects/{gitlab_id}",
    response_model=ProjectDetail,
    summary="One discovered project, its groups and collection freshness",
    description=_DETAIL_DOC
    + _STATE_DOC
    + " `monitoring` says whether it is out of monitoring scope; such a project is not "
    "evaluated (`not_evaluated: excluded`).",
    responses={404: {"description": "The project is not in the inventory"}},
)
def read_project(
    gitlab_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    as_of: Annotated[
        AwareDatetime | None,
        Query(description="Report monitoring scope and the evaluation run as of this moment."),
    ] = None,
) -> ProjectDetail:
    configured = _instance_row(db)
    project = (
        db.scalars(
            select(GitLabProject).where(
                GitLabProject.instance_id == configured[1].id,
                GitLabProject.gitlab_id == gitlab_id,
            )
        ).first()
        if configured
        else None
    )
    if configured is None or project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project in the inventory")
    settings, instance = configured
    start = None if project.namespace_kind == "user" else project.namespace_gitlab_id
    out = _project_out(project, coverage(db, instance.id, as_of))
    return ProjectDetail(
        project=out,
        ancestry=_ancestry_out(db, instance.id, start, set()),
        collection=kind_freshness(db, instance.id, "projects", settings),
        not_evaluated=not_evaluated(project) or ("excluded" if out.monitoring.excluded else None),
        as_of=as_of,
        evaluation_run=run_ref(run_at(db, instance.id, as_of)),
    )


@router.get(
    "/freshness",
    response_model=FreshnessReport,
    summary="Whether collected GitLab data is current, per kind",
    description="Per kind (`capabilities`, `groups`, `projects`): `never`, `failed` (the latest "
    "run failed, even if an earlier one succeeded), `stale` (last success older than "
    "`stale_after_minutes`), `partial` (some resources could not be confirmed) or `fresh`. "
    "`status` is the worst of the three. `resources` counts inventory by state and errors.",
)
def read_freshness(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
) -> FreshnessReport:
    settings = _configured()
    instance_id = instance_id_for(db, settings.url) if settings else None
    return report(db, instance_id, settings)


class RunPage(BaseModel):
    items: list[RunOut]
    next_before_id: int | None


@router.get(
    "/runs",
    response_model=RunPage,
    summary="Collection run history, newest first",
    description="One row per job attempt with its outcome counts and error.",
)
def list_runs(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    kind: Annotated[
        Literal["capabilities", "groups", "projects", "security_policies"] | None, Query()
    ] = None,
    run_status: Annotated[
        Literal["running", "succeeded", "partial", "failed"] | None, Query(alias="status")
    ] = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunPage:
    url = _configured_url()
    instance_id = instance_id_for(db, url) if url else None
    if instance_id is None:
        return RunPage(items=[], next_before_id=None)
    statement = (
        select(CollectionRun)
        .where(CollectionRun.instance_id == instance_id)
        .order_by(CollectionRun.id.desc())
        .limit(limit + 1)
    )
    if kind is not None:
        statement = statement.where(CollectionRun.kind == kind)
    if run_status is not None:
        statement = statement.where(CollectionRun.status == run_status)
    if before_id is not None:
        statement = statement.where(CollectionRun.id < before_id)
    rows = db.scalars(statement).all()
    items = [RunOut.model_validate(row, from_attributes=True) for row in rows[:limit]]
    return RunPage(items=items, next_before_id=items[-1].id if len(rows) > limit else None)
