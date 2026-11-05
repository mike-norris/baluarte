"""Changes on monitored default branches with the evidence recorded for each.

See docs/CHANGE_EVIDENCE.md.

Reading needs ``findings:read`` (every role). A change is a merged merge request, a direct push
or a production deployment; each is backed by observations, newest first in its detail. With
``as_of``, a change is shown as it stood then: only changes recorded by then, each from its
newest observation collected by then and judged from it.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import Select, not_, select, tuple_
from sqlalchemy.orm import Session

from baluarte.api.v1.evaluations import ProjectRef
from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.changes import assess
from baluarte.changes.records import history, outcome_at, versions_at
from baluarte.db.models import ChangeRecord, GitLabInstance, GitLabProject, Observation
from baluarte.evidence.as_of import RunRef, run_at, run_ref
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.scope.exclusions import coverage
from baluarte.settings import ConfigurationError

router = APIRouter(tags=["changes"])

ChangeKind = Literal["merge_request", "direct_push", "deployment"]
Outcome = Literal["pass", "fail", "unknown"]
AsOf = Annotated[
    AwareDatetime | None,
    Query(
        description="Show the changes as they stood at this moment: those recorded by then, "
        "each from its newest observation collected by then."
    ),
]


class ObservationRef(BaseModel):
    id: int
    payload_digest: str | None
    collected_at: datetime


class Approver(BaseModel):
    username: str | None
    approved_at: str | None


class Unknown(BaseModel):
    status: Literal["unknown"] = "unknown"
    reason: str


class Approvals(BaseModel):
    status: Literal["known"] = "known"
    approved_by: list[Approver]
    # Enterprise Edition only; null on Community Edition and for direct pushes.
    required: int | None
    left: int | None
    self_approved: bool | None
    # Never known: GitLab lists commits without usernames.
    committer_approved: bool | None


class Tests(BaseModel):
    total: int | None
    success: int | None
    failed: int | None
    skipped: int | None
    error: int | None


class Pipeline(BaseModel):
    status: Literal["known"] = "known"
    id: int
    pipeline_status: str | None
    source: str | None
    finished_at: str | None
    # The test report summary; null when the pipeline produced none or it could not be read.
    tests: Tests | None


class NoPipeline(BaseModel):
    status: Literal["none"] = "none"


class Deployment(BaseModel):
    environment: str | None
    status: str | None
    finished_at: str | None


class Deployments(BaseModel):
    status: Literal["known"] = "known"
    items: list[Deployment]


class ChangeOut(BaseModel):
    id: int
    project: ProjectRef
    kind: ChangeKind
    # Merge requests only.
    iid: int | None
    # The commit on the default branch (merge or squash commit), or the deployed commit.
    sha: str
    title: str | None
    web_path: str | None
    author: str | None
    merged_by: str | None
    changed_at: datetime
    outcome: Outcome
    reasons: list[str]
    observation: ObservationRef
    # Null for deployments, which are not reviewed themselves.
    approvals: Approvals | Unknown | None
    pipeline: Pipeline | NoPipeline | Unknown | None
    deployments: Deployments | Unknown
    # Every commit that identifies the change.
    shas: list[str]
    # False while a part could not be read; it is read again on the next collection.
    complete: bool


class ChangeVersion(BaseModel):
    observation_id: int
    collected_at: datetime
    source: str
    payload_digest: str | None
    digest_valid: bool
    payload: dict[str, Any] | None


class ChangeDetail(ChangeOut):
    # Every observation of the change (by ``as_of``), newest first.
    versions: list[ChangeVersion]
    as_of: datetime | None = None
    evaluation_run: RunRef | None = None


class ChangePage(BaseModel):
    items: list[ChangeOut]
    next_before_id: int | None
    as_of: datetime | None = None
    evaluation_run: RunRef | None = None


def _instance_id(db: Session) -> int | None:
    settings: GitLabSettings | None
    try:
        settings = load_gitlab_settings()
    except ConfigurationError:
        settings = None
    if settings is None:
        return None
    return db.scalars(
        select(GitLabInstance.id).where(GitLabInstance.base_url == settings.url)
    ).first()


def _why(part: Mapping[str, Any], what: str) -> Unknown:
    error = part.get("error")
    state = part.get("status")
    if state == "not_supported":
        return Unknown(reason=f"{what} cannot be read with this token or GitLab edition")
    return Unknown(reason=f"{what} could not be read ({state}{': ' + error if error else ''})")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def approvals_out(payload: Mapping[str, Any]) -> Approvals | Unknown | None:
    if payload.get("kind") == "deployment":
        return None
    if payload.get("kind") == "direct_push":
        return Approvals(
            approved_by=[], required=None, left=None, self_approved=None, committer_approved=None
        )
    part = payload.get("approvals")
    if not isinstance(part, dict) or part.get("status") != "ok":
        return _why(part if isinstance(part, dict) else {}, "Approvals")
    return Approvals(
        approved_by=[
            Approver(username=entry.get("username"), approved_at=entry.get("approved_at"))
            for entry in part.get("approved_by") or []
            if isinstance(entry, dict)
        ],
        required=_int(part.get("approvals_required")),
        left=_int(part.get("approvals_left")),
        self_approved=part.get("self_approved"),
        committer_approved=part.get("committer_approved"),
    )


def pipeline_out(payload: Mapping[str, Any]) -> Pipeline | NoPipeline | Unknown | None:
    if payload.get("kind") == "deployment":
        return None
    part = payload.get("pipeline")
    if not isinstance(part, dict):
        return _why({}, "The pipeline")
    if part.get("status") == "none":
        return NoPipeline()
    if part.get("status") != "ok" or _int(part.get("id")) is None:
        return _why(part, "The pipeline")
    tests = part.get("tests")
    return Pipeline(
        id=part["id"],
        pipeline_status=part.get("pipeline_status"),
        source=part.get("source"),
        finished_at=part.get("finished_at"),
        tests=Tests.model_validate(tests) if isinstance(tests, dict) else None,
    )


def deployments_out(
    payload: Mapping[str, Any], deployed: Sequence[Mapping[str, Any]]
) -> Deployments | Unknown:
    """The change's production deployments: its own part, else the recorded deployments of its
    commits; a deployment lists itself."""
    if payload.get("kind") == "deployment":
        deployed = [payload]
    else:
        part = payload.get("deployments")
        if isinstance(part, dict):
            if part.get("status") != "ok":
                return _why(part, "Deployments")
            deployed = [item for item in part.get("items") or [] if isinstance(item, dict)]
    return Deployments(
        items=[
            Deployment(
                environment=item.get("environment"),
                status=item.get("status"),
                finished_at=item.get("finished_at"),
            )
            for item in deployed
        ]
    )


def change_out(
    record: ChangeRecord,
    project: GitLabProject,
    observation: Observation,
    judged: tuple[str, list[str]],
    deployed: Sequence[Mapping[str, Any]],
) -> ChangeOut:
    payload: Mapping[str, Any] = observation.payload or {"kind": record.kind}
    outcome, reasons = judged
    return ChangeOut(
        id=record.id,
        project=ProjectRef(
            gitlab_id=project.gitlab_id, path=project.path_with_namespace, state=project.state
        ),
        kind=record.kind,  # type: ignore[arg-type]
        iid=record.iid,
        sha=record.sha,
        title=payload.get("title"),
        web_path=payload.get("web_path"),
        author=payload.get("author") or payload.get("author_name"),
        merged_by=payload.get("merged_by"),
        changed_at=record.changed_at,
        outcome=outcome,  # type: ignore[arg-type]
        reasons=reasons,
        observation=ObservationRef(
            id=observation.id,
            payload_digest=observation.payload_digest,
            collected_at=observation.collected_at,
        ),
        approvals=approvals_out(payload),
        pipeline=pipeline_out(payload),
        deployments=deployments_out(payload, deployed),
        shas=list(record.shas),
        complete=not assess.incomplete(payload) if observation.payload else record.complete,
    )


def _deployed(
    db: Session, rows: Sequence[tuple[ChangeRecord, GitLabProject]], as_of: datetime | None
) -> dict[tuple[int, str], list[dict[str, Any]]]:
    """Recorded deployments of the page's commits, by (project, SHA)."""
    wanted = {(record.project_id, sha) for record, _ in rows for sha in record.shas}
    if not wanted:
        return {}
    statement = (
        select(ChangeRecord, GitLabProject)
        .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
        .where(
            ChangeRecord.kind == "deployment",
            tuple_(ChangeRecord.project_id, ChangeRecord.sha).in_(list(wanted)),
        )
    )
    if as_of is not None:
        statement = statement.where(ChangeRecord.created_at <= as_of)
    found = list(db.execute(statement.order_by(ChangeRecord.changed_at)).all())
    versions = _versions(db, found, as_of)
    by_commit: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for record, _ in found:
        observation = versions.get(record.id)
        if observation is not None and observation.payload is not None:
            by_commit.setdefault((record.project_id, record.sha), []).append(observation.payload)
    return by_commit


def _versions(
    db: Session, rows: Sequence[tuple[ChangeRecord, GitLabProject]], as_of: datetime | None
) -> dict[int, Observation]:
    if as_of is not None:
        return versions_at(db, rows, as_of)
    ids = [record.observation_id for record, _ in rows]
    observations = {o.id: o for o in db.scalars(select(Observation).where(Observation.id.in_(ids)))}
    return {record.id: observations[record.observation_id] for record, _ in rows}


def _page_out(
    db: Session, rows: Sequence[tuple[ChangeRecord, GitLabProject]], as_of: datetime | None
) -> list[ChangeOut]:
    versions = _versions(db, rows, as_of)
    deployed = _deployed(db, rows, as_of)
    out: list[ChangeOut] = []
    for record, project in rows:
        observation = versions.get(record.id)
        if observation is None:
            continue
        if as_of is not None and observation.payload is not None:
            judged = outcome_at(db, record, project, observation.payload, as_of)
        else:
            judged = (record.outcome, list(record.reasons))
        commits = [
            item for sha in record.shas for item in deployed.get((record.project_id, sha), [])
        ]
        out.append(change_out(record, project, observation, judged, commits))
    return out


def _changes(db: Session, instance_id: int | None) -> Select[ChangeRecord, GitLabProject]:
    return (
        select(ChangeRecord, GitLabProject)
        .join(GitLabProject, GitLabProject.id == ChangeRecord.project_id)
        .where(ChangeRecord.instance_id == (instance_id or 0))
    )


_CHUNK = 200


@router.get(
    "/changes",
    response_model=ChangePage,
    summary="Changes on monitored default branches, newest first",
    description="Merged merge requests and direct pushes (and, with `kind=deployment`, production "
    "deployments), each with its approvals, pipeline, deployments and outcome under the default "
    "checks: at least one approval, no self-approval and no direct push; a passing pipeline. A "
    "part GitLab did not answer is `unknown`, never a pass. Filter by `project` (GitLab ID), "
    "`kind`, `outcome` and the `from`/`to` range of when the change landed. Projects excluded "
    "from monitoring are left out unless named with `project`.",
)
def list_changes(
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    project: Annotated[int | None, Query(gt=0)] = None,
    kind: ChangeKind | None = None,
    outcome: Outcome | None = None,
    changed_from: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    changed_to: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    as_of: AsOf = None,
) -> ChangePage:
    instance_id = _instance_id(db)
    statement = _changes(db, instance_id)
    if kind is not None:
        statement = statement.where(ChangeRecord.kind == kind)
    else:
        statement = statement.where(ChangeRecord.kind != "deployment")
    if project is not None:
        statement = statement.where(GitLabProject.gitlab_id == project)
    else:
        # Like the other lists, projects excluded from monitoring are left out unless named.
        found = coverage(db, instance_id)
        if not found.empty:
            statement = statement.where(not_(found.project_excluded()))
    if changed_from is not None:
        statement = statement.where(ChangeRecord.changed_at >= changed_from)
    if changed_to is not None:
        statement = statement.where(ChangeRecord.changed_at <= changed_to)
    if as_of is not None:
        statement = statement.where(ChangeRecord.created_at <= as_of)
    elif outcome is not None:
        statement = statement.where(ChangeRecord.outcome == outcome)
    if before_id is not None:
        before = db.get(ChangeRecord, before_id)
        if before is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "before_id is unknown")
        statement = statement.where(
            tuple_(ChangeRecord.changed_at, ChangeRecord.id) < (before.changed_at, before.id)
        )
    statement = statement.order_by(ChangeRecord.changed_at.desc(), ChangeRecord.id.desc())
    items: list[ChangeOut] = []
    more = False
    offset = 0
    # With ``as_of`` the outcome then is computed, so it is filtered here.
    while not more:
        rows = list(db.execute(statement.offset(offset).limit(_CHUNK)).all())
        offset += len(rows)
        for item in _page_out(db, rows, as_of):
            if outcome is not None and item.outcome != outcome:
                continue
            if len(items) == limit:
                more = True
                break
            items.append(item)
        if len(rows) < _CHUNK:
            break
    return ChangePage(
        items=items,
        next_before_id=items[-1].id if more else None,
        as_of=as_of,
        evaluation_run=run_ref(run_at(db, instance_id, as_of)),
    )


@router.get(
    "/changes/{change_id}",
    response_model=ChangeDetail,
    summary="One change with every observation recorded for it",
    description="Each version is an observation of the change, newest first; `digest_valid` "
    "recomputes the SHA-256 of its payload. A late deployment or a part read again later adds a "
    "version and never rewrites an earlier one.",
    responses={404: {"description": "No such change (recorded by `as_of`)"}},
)
def read_change(
    change_id: Annotated[int, Path(gt=0)],
    _: Annotated[Principal, Depends(require(Permission.FINDINGS_READ))],
    db: Annotated[Session, Depends(database)],
    as_of: AsOf = None,
) -> ChangeDetail:
    instance_id = _instance_id(db)
    row = db.execute(_changes(db, instance_id).where(ChangeRecord.id == change_id)).first()
    if row is None or (as_of is not None and row[0].created_at > as_of):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change")
    found = _page_out(db, [row], as_of)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such change")
    out = found[0]
    versions = [
        o for o in history(db, row[0], row[1].gitlab_id) if as_of is None or o.collected_at <= as_of
    ]
    return ChangeDetail(
        **dict(out),
        versions=[
            ChangeVersion(
                observation_id=o.id,
                collected_at=o.collected_at,
                source=o.source,
                payload_digest=o.payload_digest,
                digest_valid=verify(o),
                payload=o.payload,
            )
            for o in versions
        ],
        as_of=as_of,
        evaluation_run=run_ref(run_at(db, instance_id, as_of)),
    )
