"""Discover the GitLab security policies that reach each group and project (read-only).

GitLab security policies (scan execution, merge request approval, pipeline execution and
vulnerability management) live in a security policy project linked to a group or project, and
apply to everything below it. Baluarte reads them with GraphQL queries sent as GET
(``GitLabClient.graphql``); it never creates, links or changes a policy.

For every active group and project, one query reads:

- ``securityPolicyProject``: the policy project linked directly to it, if any.
- For each policy type the capability probe found supported, ``relationship: INHERITED``: the
  policies defined for the target and its ancestor groups. For a project the query also passes
  ``includeUnscoped: false``, so GitLab returns only the policies whose ``policy_scope`` includes
  the project (GitLab applies the scope, compliance frameworks included, not Baluarte).

Each target gets one observation, with the allowlisted shape in ``_payload``: per type a status,
whether the listing was cut short, and the policies with their name, description, enabled flag,
verbatim YAML, update time, source and scope. A type the probe found unsupported is recorded as
``unsupported``, and one it could not check as ``unknown``; neither is ever read as "no policy".
A target GitLab would not answer for is recorded without a payload, like branch protection.

After every target is read, the catalogue (``gitlab_security_policy``) is rebuilt: one row per
policy, keyed by the security policy project that holds it. A policy no target reported is
marked ``missing`` only when every target was read and every type was readable.
"""

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte.db.models import (
    GitLabGroup,
    GitLabProject,
    GitLabSecurityPolicy,
)
from baluarte.evidence.observations import Observed, Provenance, TargetColumn, record
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
    UnsupportedField,
)
from baluarte.scope.exclusions import coverage

logger = logging.getLogger(__name__)

PolicyType = Literal["scan_execution", "approval", "pipeline_execution", "vulnerability_management"]
TargetKind = Literal["group", "project"]
# Per type in an observation: read, unavailable on this GitLab, or not checked.
TypeStatus = Literal["ok", "unsupported", "unknown"]

# GraphQL field for each policy type, on both Group and Project.
FIELDS: Mapping[str, str] = {
    "scan_execution": "scanExecutionPolicies",
    "approval": "approvalPolicies",
    "pipeline_execution": "pipelineExecutionPolicies",
    "vulnerability_management": "vulnerabilityManagementPolicies",
}

GROUP_SOURCE = "GET /api/graphql group security policies"
PROJECT_SOURCE = "GET /api/graphql project security policies"
SOURCES: Mapping[TargetKind, str] = {"group": GROUP_SOURCE, "project": PROJECT_SOURCE}

READ_PROBE = "security_policies.read"


def type_probe(policy_type: str) -> str:
    return f"security_policies.{policy_type}"


# GitLab caps connection pages at 100. A policy project holds a handful of policies per type,
# so a full page means the listing may be cut short and is recorded as such.
PAGE = 100
SCOPE_PAGE = 50
TYPES_PER_QUERY = 2
MAX_CONSECUTIVE_FAILURES = 20
BATCH = 50

_PATHS = "nodes { fullPath } pageInfo { hasNextPage }"
_SCOPE = (
    "policyScope { "
    f"complianceFrameworks(first: {SCOPE_PAGE}) {{ nodes {{ name }} pageInfo {{ hasNextPage }} }} "
    f"includingProjects(first: {SCOPE_PAGE}) {{ {_PATHS} }} "
    f"excludingProjects(first: {SCOPE_PAGE}) {{ {_PATHS} }} "
    f"includingGroups(first: {SCOPE_PAGE}) {{ {_PATHS} }} "
    f"excludingGroups(first: {SCOPE_PAGE}) {{ {_PATHS} }} "
    "excludingArchivedProjects excludingPersonalProjects }"
)
_POLICY = (
    "name description enabled yaml updatedAt "
    "source { __typename "
    "... on GroupSecurityPolicySource { inherited namespace { fullPath } } "
    "... on ProjectSecurityPolicySource { project { fullPath } } } " + _SCOPE
)


def _type_selection(kind: TargetKind, policy_type: str, first: int = PAGE) -> str:
    arguments = f"relationship: INHERITED, first: {first}"
    if kind == "project":
        arguments += ", includeUnscoped: false"
    return (
        f"{policy_type}: {FIELDS[policy_type]}({arguments}) "
        f"{{ nodes {{ {_POLICY} }} pageInfo {{ hasNextPage }} }}"
    )


def target_query(kind: TargetKind, types: Sequence[str]) -> str:
    """One query for a group or project; ``$path`` is its full path."""
    selections = " ".join(_type_selection(kind, policy_type) for policy_type in types)
    return (
        f"query($path: ID!) {{ target: {kind}(fullPath: $path) {{ "
        f"securityPolicyProject {{ fullPath }} {selections} }} }}"
    )


def target_queries(kind: TargetKind, types: Sequence[str]) -> list[str]:
    """The queries that read a target: all four types in one query exceed GitLab's default
    query complexity limit (about 87 per type; the limit is 300 for a signed-in caller)."""
    chunks = [types[i : i + TYPES_PER_QUERY] for i in range(0, len(types), TYPES_PER_QUERY)]
    return [target_query(kind, chunk) for chunk in chunks or [[]]]


def link_probe_query() -> str:
    return "query($path: ID!) { group(fullPath: $path) { securityPolicyProject { fullPath } } }"


def type_probe_query(policy_type: str) -> str:
    """The same selection discovery uses, for one page of one policy, on a sample group."""
    return (
        "query($path: ID!) { group(fullPath: $path) { "
        f"{_type_selection('group', policy_type, first=1)} }} }}"
    )


def type_availability(snapshot: Mapping[str, Any] | None) -> dict[str, TypeStatus]:
    """Per policy type, from the latest capability snapshot.

    ``ok`` when the probe ran the type's query successfully, ``unsupported`` when GitLab does not
    have the field, and ``unknown`` otherwise (forbidden, an error, skipped or never probed):
    a tier or license that could not be read is unknown, never "not Ultimate".
    """
    unknown: dict[str, TypeStatus] = dict.fromkeys(FIELDS, "unknown")
    if not isinstance(snapshot, Mapping) or snapshot.get("status") == "failed":
        return unknown
    probes = snapshot.get("probes")
    if not isinstance(probes, Mapping):
        return unknown
    result: dict[str, TypeStatus] = {}
    for policy_type in FIELDS:
        probe = probes.get(type_probe(policy_type))
        status = probe.get("status") if isinstance(probe, Mapping) else None
        result[policy_type] = (
            "ok"
            if status == "supported"
            else "unsupported"
            if status == "unsupported"
            else "unknown"
        )
    return result


def readable(snapshot: Mapping[str, Any] | None) -> bool:
    """Whether discovery can run: links are readable and at least one policy type is."""
    probes = snapshot.get("probes") if isinstance(snapshot, Mapping) else None
    if not isinstance(probes, Mapping):
        return False
    link = probes.get(READ_PROBE)
    linked = isinstance(link, Mapping) and link.get("status") == "supported"
    return linked and "ok" in type_availability(snapshot).values()


def _path(node: Any) -> str | None:
    value = node.get("fullPath") if isinstance(node, dict) else None
    return value if isinstance(value, str) else None


def _connection_paths(connection: Any, key: str = "fullPath") -> tuple[list[str], bool]:
    if not isinstance(connection, dict):
        return [], False
    nodes = connection.get("nodes")
    values = sorted(
        {
            node[key]
            for node in (nodes if isinstance(nodes, list) else [])
            if isinstance(node, dict) and isinstance(node.get(key), str)
        }
    )
    info = connection.get("pageInfo")
    more = isinstance(info, dict) and info.get("hasNextPage") is True
    return values, more


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _scope(raw: Any) -> dict[str, Any]:
    scope = raw if isinstance(raw, dict) else {}
    truncated = False
    result: dict[str, Any] = {}
    for key, name, attribute in (
        ("complianceFrameworks", "compliance_frameworks", "name"),
        ("includingProjects", "including_projects", "fullPath"),
        ("excludingProjects", "excluding_projects", "fullPath"),
        ("includingGroups", "including_groups", "fullPath"),
        ("excludingGroups", "excluding_groups", "fullPath"),
    ):
        values, more = _connection_paths(scope.get(key), attribute)
        result[name] = values
        truncated = truncated or more
    result["excluding_archived_projects"] = _flag(scope.get("excludingArchivedProjects"))
    result["excluding_personal_projects"] = _flag(scope.get("excludingPersonalProjects"))
    result["truncated"] = truncated
    return result


def _source(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    typename = source.get("__typename")
    if typename == "GroupSecurityPolicySource":
        return {
            "type": "group",
            "path": _path(source.get("namespace")),
            "inherited": _flag(source.get("inherited")),
        }
    if typename == "ProjectSecurityPolicySource":
        return {"type": "project", "path": _path(source.get("project")), "inherited": None}
    return {"type": None, "path": None, "inherited": None}


def _policy(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _text(node.get("name")),
        "description": _text(node.get("description")),
        "enabled": _flag(node.get("enabled")),
        # Verbatim: it is the policy as GitLab enforces it. Policy YAML holds rules, branch names
        # and approver usernames, not credentials.
        "yaml": _text(node.get("yaml")),
        "updated_at": _text(node.get("updatedAt")),
        "source": _source(node.get("source")),
        "scope": _scope(node.get("policyScope")),
    }


def payload(target: dict[str, Any], availability: Mapping[str, TypeStatus]) -> dict[str, Any]:
    """The allowlisted observation payload for one target's query result."""
    types: dict[str, Any] = {}
    for policy_type, status in availability.items():
        if status != "ok":
            types[policy_type] = {"status": status, "truncated": False, "policies": []}
            continue
        connection = target.get(policy_type)
        if not isinstance(connection, dict):
            types[policy_type] = {"status": "unknown", "truncated": False, "policies": []}
            continue
        nodes = connection.get("nodes")
        policies = [_policy(node) for node in (nodes if isinstance(nodes, list) else [])]
        policies.sort(
            key=lambda p: (p["source"]["path"] or "", p["name"] or "", p["updated_at"] or "")
        )
        info = connection.get("pageInfo")
        types[policy_type] = {
            "status": "ok",
            "truncated": isinstance(info, dict) and info.get("hasNextPage") is True,
            "policies": policies,
        }
    return {
        "security_policy_project": _path(target.get("securityPolicyProject")),
        "types": types,
    }


def complete(observed_payload: Mapping[str, Any]) -> bool:
    """Every type was read in full."""
    types = observed_payload.get("types")
    if not isinstance(types, Mapping):
        return False
    return all(
        isinstance(entry, Mapping) and entry.get("status") == "ok" and not entry.get("truncated")
        for entry in types.values()
    )


class TokenRejected(Exception):
    def __init__(self, observed: Observed) -> None:
        super().__init__(observed.error)
        self.observed = observed


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    row_id: int
    path: str


def _unread(target: Target, status: str, error: str) -> Observed:
    return Observed(
        target_id=target.row_id,
        source=SOURCES[target.kind],
        source_ref=target.path,
        collection_status=status,
        error=error,
    )


def read_target(
    client: GitLabClient, target: Target, availability: Mapping[str, TypeStatus]
) -> Observed:
    """One target's security policies, or why they could not be read.

    Raises TokenRejected, carrying the observation to record, when GitLab answers 401.
    """
    types = [policy_type for policy_type, status in availability.items() if status == "ok"]
    found: dict[str, Any] = {}
    try:
        for query in target_queries(target.kind, types):
            part = client.graphql(query, {"path": target.path}).get("target")
            if not isinstance(part, dict):
                return _unread(
                    target, "not_found", f"GraphQL returned no {target.kind} for this path"
                )
            found.update(part)
    except Unauthorized as error:
        raise TokenRejected(_unread(target, "forbidden", str(error))) from error
    except Forbidden as error:
        return _unread(target, "forbidden", str(error))
    except NotFound as error:
        return _unread(target, "not_found", str(error))
    except UnsupportedField as error:
        return _unread(target, "unsupported", str(error))
    except GitLabError as error:
        return _unread(target, "failed", str(error))
    body = payload(found, availability)
    return Observed(
        target_id=target.row_id,
        source=SOURCES[target.kind],
        source_ref=target.path,
        collection_status="ok" if complete(body) else "partial",
        payload=body,
    )


@dataclass
class Reads:
    read: int = 0
    unread: int = 0

    def as_counts(self) -> dict[str, int]:
        return {"read": self.read, "unread": self.unread}


@dataclass
class Discovery:
    groups: Reads = field(default_factory=Reads)
    projects: Reads = field(default_factory=Reads)
    # Targets read whose listing of some type was unknown, unsupported or cut short.
    incomplete: int = 0
    policies: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)

    @property
    def seen(self) -> int:
        return self.groups.read + self.projects.read

    @property
    def unread(self) -> int:
        return self.groups.unread + self.projects.unread


def _targets(engine: Engine, instance_id: int) -> list[Target]:
    """Active groups and projects in monitoring scope (``baluarte.scope``)."""
    with Session(engine) as db:
        scope = coverage(db, instance_id)
        groups = db.execute(
            select(GitLabGroup.id, GitLabGroup.full_path)
            .where(
                GitLabGroup.instance_id == instance_id,
                GitLabGroup.state == "active",
                ~scope.group_excluded(),
            )
            .order_by(GitLabGroup.id)
        ).all()
        projects = db.execute(
            select(GitLabProject.id, GitLabProject.path_with_namespace)
            .where(
                GitLabProject.instance_id == instance_id,
                GitLabProject.state == "active",
                scope.project_included(),
            )
            .order_by(GitLabProject.id)
        ).all()
    return [Target("group", row_id, path) for row_id, path in groups] + [
        Target("project", row_id, path) for row_id, path in projects
    ]


_COLUMNS: Mapping[TargetKind, TargetColumn] = {"group": "group_id", "project": "project_id"}


def collect(
    client: GitLabClient,
    engine: Engine,
    instance_id: int,
    availability: Mapping[str, TypeStatus],
    provenance: Provenance | None = None,
) -> Discovery:
    """Read and record every active target's security policies, then rebuild the catalogue."""
    result = Discovery()
    pending: dict[TargetKind, list[Observed]] = {"group": [], "project": []}
    read: list[Observed] = []
    stop_reason: str | None = None
    failures = 0

    def flush(kind: TargetKind) -> None:
        with Session(engine) as db:
            record(db, instance_id, _COLUMNS[kind], pending[kind], provenance)
            db.commit()
        pending[kind].clear()

    for target in _targets(engine, instance_id):
        if stop_reason is not None:
            observed = _unread(target, "failed", stop_reason)
        else:
            try:
                observed = read_target(client, target, availability)
            except TokenRejected as rejected:
                observed = rejected.observed
                stop_reason = "not read: GitLab rejected the token earlier in this collection"
        reads = result.groups if target.kind == "group" else result.projects
        if observed.payload is not None:
            reads.read += 1
            failures = 0
            read.append(observed)
            if observed.collection_status != "ok":
                result.incomplete += 1
        else:
            reads.unread += 1
            failures += 1
            if stop_reason is None and failures >= MAX_CONSECUTIVE_FAILURES:
                stop_reason = (
                    f"not read: {MAX_CONSECUTIVE_FAILURES} security policy reads in a row "
                    "failed earlier in this collection"
                )
        pending[target.kind].append(observed)
        if len(pending[target.kind]) >= BATCH:
            flush(target.kind)
    for kind in ("group", "project"):
        if pending[kind]:
            flush(kind)

    confirmed = result.unread == 0 and result.incomplete == 0
    run_id = provenance.collection_run_id if provenance else None
    with Session(engine) as db:
        result.policies = rebuild_catalogue(db, instance_id, read, confirmed, run_id)
        result.by_type = dict(
            db.execute(
                select(GitLabSecurityPolicy.policy_type, func.count())
                .where(
                    GitLabSecurityPolicy.instance_id == instance_id,
                    GitLabSecurityPolicy.state == "active",
                )
                .group_by(GitLabSecurityPolicy.policy_type)
            ).all()
        )
        db.commit()
    if result.unread:
        logger.warning("gitlab: security policies not read for %s targets", result.unread)
    return result


@dataclass
class _Entry:
    policy_type: str
    name: str
    origin_kind: str
    origin_path: str
    policy: dict[str, Any]
    linked_from: set[tuple[str, str]] = field(default_factory=set)


Links = dict[tuple[str, str], str]


def links(read: Sequence[tuple[str, str | None, Mapping[str, Any] | None]]) -> Links:
    """(kind, path) of each target read to the policy project linked directly to it.

    ``read`` holds (source, path, payload) for each target's observation.
    """
    found: Links = {}
    for source, path, body in read:
        linked = body.get("security_policy_project") if body else None
        if isinstance(linked, str) and path:
            found[("group" if source == GROUP_SOURCE else "project", path)] = linked
    return found


def origin(policy: Mapping[str, Any], known: Links) -> tuple[str, str] | None:
    """Where a listed policy lives: (origin_kind, origin_path), or None without a source path.

    The policy project linked to the policy's source when Baluarte read that source, otherwise
    the source group or project itself (``linked_source``).
    """
    source = policy.get("source")
    path = source.get("path") if isinstance(source, Mapping) else None
    if not isinstance(path, str) or not isinstance(source, Mapping):
        return None
    project = known.get((str(source.get("type")), path))
    return ("policy_project", project) if project else ("linked_source", path)


def _triples(read: Sequence[Observed]) -> list[tuple[str, str | None, Mapping[str, Any] | None]]:
    return [(observed.source, observed.source_ref, observed.payload) for observed in read]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def catalogue(read: Sequence[Observed]) -> list[_Entry]:
    """Distinct policies across every target read, each with the sources it is linked through."""
    known = links(_triples(read))
    entries: dict[tuple[str, str, str], _Entry] = {}
    for observed in read:
        types = observed.payload.get("types", {}) if observed.payload else {}
        for policy_type, listing in types.items():
            for policy in listing.get("policies", []):
                name = policy.get("name")
                found = origin(policy, known)
                if not isinstance(name, str) or found is None:
                    continue
                origin_kind, origin_path = found
                source_key = (str(policy["source"]["type"]), policy["source"]["path"])
                key = (policy_type, origin_path, name)
                entry = entries.get(key)
                if entry is None or (policy.get("updated_at") or "") > (
                    entry.policy.get("updated_at") or ""
                ):
                    previous = entry.linked_from if entry else set()
                    entry = _Entry(policy_type, name, origin_kind, origin_path, policy, previous)
                    entries[key] = entry
                entry.linked_from.add(source_key)
    return sorted(entries.values(), key=lambda e: (e.policy_type, e.origin_path, e.name))


def yaml_digest(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def rebuild_catalogue(
    db: Session,
    instance_id: int,
    read: Sequence[Observed],
    confirmed: bool,
    run_id: int | None,
) -> dict[str, int]:
    """Upsert the policies seen; mark the rest missing only when the run saw everything."""
    existing = {
        (row.policy_type, row.origin_path, row.name): row
        for row in db.scalars(
            select(GitLabSecurityPolicy).where(GitLabSecurityPolicy.instance_id == instance_id)
        )
    }
    now: datetime = db.execute(select(func.now())).scalar_one()
    counts = {"active": 0, "new": 0, "missing": 0}
    seen: set[tuple[str, str, str]] = set()
    for entry in catalogue(read):
        key = (entry.policy_type, entry.origin_path, entry.name)
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = GitLabSecurityPolicy(
                instance_id=instance_id,
                policy_type=entry.policy_type,
                name=entry.name,
                origin_path=entry.origin_path,
            )
            db.add(row)
            counts["new"] += 1
        policy = entry.policy
        row.origin_kind = entry.origin_kind
        row.description = policy.get("description")
        row.enabled = policy.get("enabled")
        row.yaml = policy.get("yaml")
        row.yaml_digest = yaml_digest(row.yaml)
        row.gitlab_updated_at = _parse_time(policy.get("updated_at"))
        row.scope = policy.get("scope") or {}
        row.linked_from = [{"type": kind, "path": path} for kind, path in sorted(entry.linked_from)]
        row.state = "active"
        row.last_seen_at = now
        row.last_collection_run_id = run_id
        counts["active"] += 1
    if confirmed:
        for key, row in existing.items():
            if key not in seen and row.state != "missing":
                row.state = "missing"
                row.last_collection_run_id = run_id
                counts["missing"] += 1
    db.flush()
    return counts
