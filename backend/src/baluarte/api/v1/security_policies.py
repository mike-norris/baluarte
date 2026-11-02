"""GitLab security policies discovered by Baluarte, and which groups and projects they reach.

Read-only views of what ``gitlab.discover_security_policies`` collected
(docs/GITLAB_INTEGRATION.md#security-policies). Nothing here calls GitLab.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.db.models import (
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    GitLabSecurityPolicy,
    Observation,
)
from baluarte.integrations.gitlab import security_policies as discovery
from baluarte.integrations.gitlab.freshness import KindFreshness, kind_freshness
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.settings import ConfigurationError

router = APIRouter(prefix="/gitlab/security-policies", tags=["gitlab"])

PolicyType = Literal["scan_execution", "approval", "pipeline_execution", "vulnerability_management"]
POLICY_TYPES: tuple[PolicyType, ...] = get_args(PolicyType)
TypeStatus = Literal["ok", "unsupported", "unknown"]
CoverageStatus = Literal["covered", "not_covered", "unknown"]


class LinkedFrom(BaseModel):
    type: Literal["group", "project"]
    path: str


class Reach(BaseModel):
    groups: int
    projects: int


class SecurityPolicySummary(BaseModel):
    id: int
    policy_type: PolicyType
    name: str
    description: str | None
    enabled: bool | None
    origin_kind: Literal["policy_project", "linked_source"]
    origin_path: str
    linked_from: list[LinkedFrom]
    state: Literal["active", "missing"]
    yaml_digest: str | None
    gitlab_updated_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    reaches: Reach


class Discovery(BaseModel):
    """Whether discovery can run and how current it is."""

    readable: bool
    availability: dict[PolicyType, TypeStatus]
    collection: KindFreshness


class SecurityPolicyPage(Discovery):
    items: list[SecurityPolicySummary]
    next_after_id: int | None


class ReachedTarget(BaseModel):
    id: int
    gitlab_id: int
    path: str
    # The group or project the policy is linked through, and whether GitLab says it is inherited.
    via: str | None
    inherited: bool | None
    observation_id: int
    collected_at: datetime


class Reached(BaseModel):
    groups: list[ReachedTarget]
    projects: list[ReachedTarget]


class SecurityPolicyDetail(SecurityPolicySummary, Discovery):
    yaml: str | None
    scope: dict[str, Any]
    reached: Reached


class PolicyRef(BaseModel):
    # The catalogue row, or null when the policy is not in the catalogue yet.
    id: int | None
    name: str | None
    enabled: bool | None
    via: str | None
    inherited: bool | None


class TypeCoverage(BaseModel):
    status: TypeStatus
    truncated: bool
    enabled: int
    disabled: int
    policies: list[PolicyRef]


class ObservationRef(BaseModel):
    id: int
    collected_at: datetime
    collection_status: str
    error: str | None


class CoverageRow(BaseModel):
    id: int
    gitlab_id: int
    path: str
    status: CoverageStatus
    reason: str | None
    security_policy_project: str | None
    observation: ObservationRef | None
    types: dict[PolicyType, TypeCoverage]


class CoverageCounts(BaseModel):
    covered: int = 0
    not_covered: int = 0
    unknown: int = 0


class Coverage(Discovery):
    summary: dict[Literal["groups", "projects"], CoverageCounts]
    groups: list[CoverageRow]
    projects: list[CoverageRow]


def _configured() -> GitLabSettings | None:
    try:
        return load_gitlab_settings()
    except ConfigurationError:
        return None


def _instance(db: Session, settings: GitLabSettings | None) -> GitLabInstance | None:
    if settings is None:
        return None
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == settings.url)).first()


def _discovery(
    db: Session, instance: GitLabInstance | None, settings: GitLabSettings | None
) -> dict[str, Any]:
    snapshot = instance.capability_snapshot if instance else None
    return {
        "readable": discovery.readable(snapshot),
        "availability": discovery.type_availability(snapshot),
        "collection": kind_freshness(
            db, instance.id if instance else None, "security_policies", settings
        ),
    }


Latest = list[tuple[str, int, int, str, Observation | None]]


def _latest(db: Session, instance_id: int) -> Latest:
    """(kind, row id, GitLab ID, path, latest observation) for every active group and project."""
    rows: Latest = []
    for kind, model, path_column, column, source in (
        ("group", GitLabGroup, GitLabGroup.full_path, Observation.group_id, discovery.GROUP_SOURCE),
        (
            "project",
            GitLabProject,
            GitLabProject.path_with_namespace,
            Observation.project_id,
            discovery.PROJECT_SOURCE,
        ),
    ):
        observations = {
            observation_target: observation
            for observation_target, observation in db.execute(
                select(column, Observation)
                .where(Observation.instance_id == instance_id, Observation.source == source)
                .ext(distinct_on(column))
                .order_by(column, Observation.collected_at.desc(), Observation.id.desc())
            )
        }
        for row_id, gitlab_id, path in db.execute(
            select(model.id, model.gitlab_id, path_column)
            .where(model.instance_id == instance_id, model.state == "active")
            .order_by(path_column)
        ):
            rows.append((kind, row_id, gitlab_id, path, observations.get(row_id)))
    return rows


def _links(latest: Latest) -> discovery.Links:
    return discovery.links(
        [
            (observation.source, path, observation.payload)
            for _, _, _, path, observation in latest
            if observation is not None
        ]
    )


Key = tuple[str, str, str]


def _listed(
    latest: Latest, known: discovery.Links
) -> list[tuple[str, int, int, str, Observation, str, Mapping[str, Any], Key | None]]:
    """Every policy listed in a latest observation, with the catalogue key it belongs to."""
    found = []
    for kind, row_id, gitlab_id, path, observation in latest:
        if observation is None or not observation.payload:
            continue
        for policy_type, listing in observation.payload.get("types", {}).items():
            for policy in listing.get("policies", []):
                where = discovery.origin(policy, known)
                name = policy.get("name")
                key = (policy_type, where[1], name) if where and isinstance(name, str) else None
                found.append((kind, row_id, gitlab_id, path, observation, policy_type, policy, key))
    return found


def _summary(row: GitLabSecurityPolicy, reach: Mapping[str, int]) -> SecurityPolicySummary:
    return SecurityPolicySummary.model_validate(
        {
            **{
                name: getattr(row, name)
                for name in (
                    "id",
                    "policy_type",
                    "name",
                    "description",
                    "enabled",
                    "origin_kind",
                    "origin_path",
                    "linked_from",
                    "state",
                    "yaml_digest",
                    "gitlab_updated_at",
                    "first_seen_at",
                    "last_seen_at",
                )
            },
            "reaches": {"groups": reach.get("group", 0), "projects": reach.get("project", 0)},
        }
    )


def _reach(latest: Latest) -> dict[Key, dict[str, int]]:
    counts: dict[Key, dict[str, int]] = {}
    seen: set[tuple[Key, str, int]] = set()
    for kind, row_id, _, _, _, _, _, key in _listed(latest, _links(latest)):
        if key is None or (key, kind, row_id) in seen:
            continue
        seen.add((key, kind, row_id))
        counts.setdefault(key, {}).setdefault(kind, 0)
        counts[key][kind] += 1
    return counts


@router.get(
    "",
    response_model=SecurityPolicyPage,
    summary="Discovered GitLab security policies",
    description="One item per policy, keyed by the security policy project that holds it "
    "(`origin_kind` `policy_project`) or, when Baluarte cannot see which project that is, the "
    "group or project it is linked to (`linked_source`). `reaches` counts the active groups "
    "and projects whose latest discovery lists the policy. `state` is `missing` when a "
    "complete discovery no longer found it. `readable`, `availability` and `collection` say "
    "whether discovery can run on this instance and how current it is.",
)
def list_security_policies(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    policy_type: Annotated[PolicyType | None, Query()] = None,
    state: Annotated[Literal["active", "missing"] | None, Query()] = None,
    enabled: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    after_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> SecurityPolicyPage:
    settings = _configured()
    instance = _instance(db, settings)
    context = _discovery(db, instance, settings)
    if instance is None:
        return SecurityPolicyPage(items=[], next_after_id=None, **context)
    statement = (
        select(GitLabSecurityPolicy)
        .where(GitLabSecurityPolicy.instance_id == instance.id)
        .order_by(GitLabSecurityPolicy.id)
        .limit(limit + 1)
    )
    if policy_type is not None:
        statement = statement.where(GitLabSecurityPolicy.policy_type == policy_type)
    if state is not None:
        statement = statement.where(GitLabSecurityPolicy.state == state)
    if enabled is not None:
        statement = statement.where(GitLabSecurityPolicy.enabled.is_(enabled))
    if search is not None:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(GitLabSecurityPolicy.name.ilike(f"%{escaped}%", escape="\\"))
    if after_id is not None:
        statement = statement.where(GitLabSecurityPolicy.id > after_id)
    rows = db.scalars(statement).all()
    reach = _reach(_latest(db, instance.id))
    items = [
        _summary(row, reach.get((row.policy_type, row.origin_path, row.name), {}))
        for row in rows[:limit]
    ]
    return SecurityPolicyPage(
        items=items, next_after_id=items[-1].id if len(rows) > limit else None, **context
    )


def _covering(rows: Sequence[GitLabSecurityPolicy]) -> dict[Key, GitLabSecurityPolicy]:
    return {(row.policy_type, row.origin_path, row.name): row for row in rows}


def _coverage_row(
    kind: str,
    row_id: int,
    gitlab_id: int,
    path: str,
    observation: Observation | None,
    known: discovery.Links,
    catalogue: Mapping[Key, GitLabSecurityPolicy],
    availability: Mapping[str, str],
) -> CoverageRow:
    types: dict[PolicyType, TypeCoverage] = {}
    listing = (observation.payload or {}).get("types", {}) if observation else {}
    for policy_type in POLICY_TYPES:
        entry = listing.get(policy_type)
        if not isinstance(entry, Mapping):
            # Not read: an unsupported type stays unsupported, anything else is unknown.
            status: TypeStatus = (
                "unsupported" if availability.get(policy_type) == "unsupported" else "unknown"
            )
            types[policy_type] = TypeCoverage(
                status=status, truncated=False, enabled=0, disabled=0, policies=[]
            )
            continue
        refs = []
        for policy in entry.get("policies", []):
            where = discovery.origin(policy, known)
            name = policy.get("name")
            row = (
                catalogue.get((policy_type, where[1], name))
                if where and isinstance(name, str)
                else None
            )
            source = policy.get("source") or {}
            refs.append(
                PolicyRef(
                    id=row.id if row else None,
                    name=name if isinstance(name, str) else None,
                    enabled=policy.get("enabled"),
                    via=source.get("path"),
                    inherited=source.get("inherited"),
                )
            )
        entry_status = entry.get("status")
        types[policy_type] = TypeCoverage(
            status=entry_status if entry_status in ("ok", "unsupported") else "unknown",
            truncated=bool(entry.get("truncated")),
            enabled=sum(1 for ref in refs if ref.enabled is True),
            disabled=sum(1 for ref in refs if ref.enabled is not True),
            policies=refs,
        )
    coverage_status, reason = _status(observation, types)
    linked = (observation.payload or {}).get("security_policy_project") if observation else None
    return CoverageRow(
        id=row_id,
        gitlab_id=gitlab_id,
        path=path,
        status=coverage_status,
        reason=reason,
        security_policy_project=linked if isinstance(linked, str) else None,
        observation=(
            ObservationRef(
                id=observation.id,
                collected_at=observation.collected_at,
                collection_status=observation.collection_status,
                error=observation.error,
            )
            if observation
            else None
        ),
        types=types,
    )


def _status(
    observation: Observation | None, types: Mapping[PolicyType, TypeCoverage]
) -> tuple[CoverageStatus, str | None]:
    """Covered by any enabled policy; not covered only when every type was read in full."""
    if observation is None:
        return "unknown", "security policies have not been read for this target"
    if observation.payload is None:
        return "unknown", f"not read: {observation.error or observation.collection_status}"
    if any(entry.enabled for entry in types.values()):
        return "covered", None
    unread = sorted(
        policy_type
        for policy_type, entry in types.items()
        if entry.status != "ok" or entry.truncated
    )
    if unread:
        return "unknown", "no enabled policy found, but not every type was read: " + ", ".join(
            unread
        )
    return "not_covered", None


@router.get(
    "/coverage",
    response_model=Coverage,
    summary="Which groups and projects GitLab security policies reach",
    description="One row per active group and project, from its latest discovery. `status` is "
    "`covered` when at least one enabled policy reaches it, `not_covered` only when every "
    "policy type was read in full and none is enabled, and `unknown` otherwise (never read, "
    "not read, or a type unsupported, unknown or cut short). A project lists only the "
    "policies whose scope GitLab says includes it. Filter with `kind` and `status`.",
)
def read_coverage(
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
    kind: Annotated[Literal["group", "project"] | None, Query()] = None,
    coverage: Annotated[CoverageStatus | None, Query(alias="status")] = None,
) -> Coverage:
    settings = _configured()
    instance = _instance(db, settings)
    context = _discovery(db, instance, settings)
    empty: dict[Literal["groups", "projects"], CoverageCounts] = {
        "groups": CoverageCounts(),
        "projects": CoverageCounts(),
    }
    if instance is None:
        return Coverage(summary=empty, groups=[], projects=[], **context)
    latest = _latest(db, instance.id)
    known = _links(latest)
    catalogue = _covering(
        db.scalars(
            select(GitLabSecurityPolicy).where(GitLabSecurityPolicy.instance_id == instance.id)
        ).all()
    )
    rows: dict[str, list[CoverageRow]] = {"group": [], "project": []}
    summary = empty
    for target_kind, row_id, gitlab_id, path, observation in latest:
        row = _coverage_row(
            target_kind,
            row_id,
            gitlab_id,
            path,
            observation,
            known,
            catalogue,
            context["availability"],
        )
        counts = summary["groups" if target_kind == "group" else "projects"]
        setattr(counts, row.status, getattr(counts, row.status) + 1)
        if (kind is None or kind == target_kind) and (coverage is None or coverage == row.status):
            rows[target_kind].append(row)
    return Coverage(summary=summary, groups=rows["group"], projects=rows["project"], **context)


@router.get(
    "/{policy_id}",
    response_model=SecurityPolicyDetail,
    summary="One discovered GitLab security policy, with its YAML and where it reaches",
    description="`yaml` is the policy exactly as GitLab returned it. `scope` is its "
    "`policy_scope`, normalized. `reached` lists the active groups and projects whose latest "
    "discovery lists the policy, with the source it reaches them through.",
    responses={404: {"description": "No such policy on the configured instance"}},
)
def read_security_policy(
    policy_id: int,
    _: Annotated[Principal, Depends(require(Permission.INVENTORY_READ))],
    db: Annotated[Session, Depends(database)],
) -> SecurityPolicyDetail:
    settings = _configured()
    instance = _instance(db, settings)
    row = db.get(GitLabSecurityPolicy, policy_id)
    if instance is None or row is None or row.instance_id != instance.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Security policy not found")
    latest = _latest(db, instance.id)
    key = (row.policy_type, row.origin_path, row.name)
    reached: dict[str, list[ReachedTarget]] = {"group": [], "project": []}
    seen: set[tuple[str, int]] = set()
    for kind, row_id, gitlab_id, path, observation, _type, policy, found in _listed(
        latest, _links(latest)
    ):
        if found != key or (kind, row_id) in seen:
            continue
        seen.add((kind, row_id))
        source = policy.get("source") or {}
        reached[kind].append(
            ReachedTarget(
                id=row_id,
                gitlab_id=gitlab_id,
                path=path,
                via=source.get("path"),
                inherited=source.get("inherited"),
                observation_id=observation.id,
                collected_at=observation.collected_at,
            )
        )
    summary = _summary(row, {kind: len(items) for kind, items in reached.items()})
    return SecurityPolicyDetail(
        **summary.model_dump(),
        **_discovery(db, instance, settings),
        yaml=row.yaml,
        scope=row.scope,
        reached=Reached(groups=reached["group"], projects=reached["project"]),
    )
