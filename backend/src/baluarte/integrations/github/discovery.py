"""GitHub discovery: the organization, its repositories, teams and memberships (roadmap M13.2).

Inventory is normalized into the provider-neutral rows the rest of Baluarte reads (ADR-0014):
the organization is a ``gitlab_group`` row and each repository a ``gitlab_project`` row (the
tables keep their first provider's names), with GitHub's IDs, so policies, scope, evaluation and
the register address them like GitLab's. An organization-owned repository's ``namespace_kind`` is
``group``. Each listing and lookup is recorded as an observation with only allowlisted fields.

Repositories are listed and reconciled by the shared engine
(``baluarte.integrations.gitlab.discovery``): one not listed again is looked up at
``/repositories/{id}``; a 404 makes it ``missing`` and a 403 ``inaccessible``. Teams and
memberships are replaced as listed: rows not listed again become ``missing``.

Every request is a GET (``baluarte.integrations.github.client``).
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import (
    VISIBILITIES,
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    ProviderMembership,
    ProviderTeam,
)
from baluarte.evidence.observations import Observed, Provenance, record
from baluarte.integrations.github.client import GitHubClient, organization_of
from baluarte.integrations.gitlab.client import InvalidResponse
from baluarte.integrations.gitlab.discovery import Discovery, ResourceKind, discover

ORG_SOURCE = "GET /orgs/:org"
REPOS_SOURCE = "GET /orgs/:org/repos"
ORG_FIELDS = ("gitlab_id", "full_path", "name", "require_two_factor_authentication")


REPOSITORY_SOURCES = (REPOS_SOURCE, "GET /repositories/:id")


def _org_row(instance_id: int, org: dict[str, Any]) -> dict[str, Any]:
    org_id = org.get("id")
    login = org.get("login")
    if not isinstance(org_id, int) or not isinstance(login, str):
        raise InvalidResponse("GET /orgs/{org} returned an organization without an id or login")
    two_factor = org.get("two_factor_requirement_enabled")
    return {
        "instance_id": instance_id,
        "gitlab_id": org_id,
        "parent_gitlab_id": None,
        "full_path": login,
        "name": str(org.get("name") or login),
        "visibility": None,
        # Shown in full only to organization owners; null when GitHub does not say.
        "require_two_factor_authentication": two_factor if isinstance(two_factor, bool) else None,
        "state": "active",
    }


def _repo_row(instance_id: int, repo: dict[str, Any]) -> dict[str, Any]:
    repo_id = repo.get("id")
    full_name = repo.get("full_name")
    owner = repo.get("owner")
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    if (
        not isinstance(repo_id, int)
        or not isinstance(full_name, str)
        or not isinstance(owner_id, int)
    ):
        raise InvalidResponse("GitHub returned a repository without an id, full_name or owner")
    visibility = repo.get("visibility")
    default_branch = repo.get("default_branch")
    return {
        "instance_id": instance_id,
        "gitlab_id": repo_id,
        "namespace_gitlab_id": owner_id,
        "namespace_kind": "group",
        "path_with_namespace": full_name,
        "name": str(repo.get("name") or full_name.rsplit("/", 1)[-1]),
        "visibility": visibility if visibility in VISIBILITIES else None,
        "archived": repo.get("archived") is True,
        "default_branch": default_branch if isinstance(default_branch, str) else None,
        "state": "active",
    }


def repositories(organization: str) -> ResourceKind:
    """The organization's repositories; sources ``REPOSITORY_SOURCES`` for every organization."""
    return ResourceKind(
        name="repository",
        model=GitLabProject,
        list_path=f"/orgs/{organization}/repos",
        list_params={"type": "all", "sort": "full_name"},
        lookup_params={},
        to_row=_repo_row,
        refreshed=(
            "namespace_gitlab_id",
            "namespace_kind",
            "path_with_namespace",
            "name",
            "visibility",
            "archived",
            "default_branch",
        ),
        target="project_id",
        evidence_fields=(
            "gitlab_id",
            "path_with_namespace",
            "name",
            "namespace_gitlab_id",
            "namespace_kind",
            "visibility",
            "archived",
            "default_branch",
        ),
        lookup_path="/repositories",
        list_source_name=REPOS_SOURCE,
    )


@dataclass
class GitHubDiscovery:
    instance_id: int
    organization_id: int
    repositories: Discovery
    teams: dict[str, int] = field(default_factory=lambda: {"listed": 0, "missing": 0})
    members: dict[str, int] = field(default_factory=lambda: {"listed": 0, "missing": 0})


def _organization(
    client: GitHubClient, engine: Engine, connection: GitLabInstance, provenance: Provenance | None
) -> int:
    login = organization_of(connection.base_url)
    row = _org_row(connection.id, client.get_json(f"/orgs/{login}"))
    with Session(engine) as db:
        statement = pg_insert(GitLabGroup).values(row)
        group_id = db.execute(
            statement.on_conflict_do_update(
                index_elements=[GitLabGroup.instance_id, GitLabGroup.gitlab_id],
                set_={
                    "full_path": statement.excluded.full_path,
                    "name": statement.excluded.name,
                    "require_two_factor_authentication": (
                        statement.excluded.require_two_factor_authentication
                    ),
                    "state": "active",
                    "last_seen_at": func.now(),
                    "last_error": None,
                    "last_error_at": None,
                },
            ).returning(GitLabGroup.id)
        ).scalar_one()
        record(
            db,
            connection.id,
            "group_id",
            [
                Observed(
                    target_id=group_id,
                    source=ORG_SOURCE,
                    source_ref=f"/orgs/{login}",
                    collection_status="ok",
                    payload={key: row[key] for key in ORG_FIELDS},
                )
            ],
            provenance,
        )
        db.commit()
    return int(row["gitlab_id"])


def _mark_missing(db: Session, model: Any, instance_id: int, started: Any, *where: Any) -> int:
    result = db.execute(
        update(model)
        .where(
            model.instance_id == instance_id,
            model.state == "active",
            model.last_seen_at < started,
            *where,
        )
        .values(state="missing")
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _members(
    client: GitHubClient,
    db: Session,
    instance_id: int,
    path: str,
    subject: tuple[str, int],
    roles: tuple[str, ...],
) -> int:
    """List members once per role (GitHub's ``role`` filter) and upsert them."""
    listed = 0
    for role in roles:
        for page in client.paginate(path, {"role": role}):
            rows = [
                {
                    "instance_id": instance_id,
                    "subject_kind": subject[0],
                    "subject_provider_id": subject[1],
                    "user_provider_id": user["id"],
                    "login": user["login"],
                    "role": role,
                    "state": "active",
                }
                for user in page
                if isinstance(user, dict)
                and isinstance(user.get("id"), int)
                and isinstance(user.get("login"), str)
            ]
            if not rows:
                continue
            statement = pg_insert(ProviderMembership).values(rows)
            db.execute(
                statement.on_conflict_do_update(
                    constraint="uq_provider_membership_member",
                    set_={
                        "login": statement.excluded.login,
                        "role": statement.excluded.role,
                        "state": "active",
                        "last_seen_at": func.now(),
                    },
                )
            )
            listed += len(rows)
    return listed


def discover_organization(
    client: GitHubClient,
    engine: Engine,
    connection: GitLabInstance,
    provenance: Provenance | None = None,
) -> GitHubDiscovery:
    """Read the organization, then its repositories, teams and memberships."""
    login = organization_of(connection.base_url)
    org_id = _organization(client, engine, connection, provenance)
    repos = discover(client, engine, connection.base_url, repositories(login), provenance)
    result = GitHubDiscovery(connection.id, org_id, repos)

    with Session(engine) as db:
        started = db.execute(select(func.now())).scalar_one()
        teams: list[dict[str, Any]] = []
        for page in client.paginate(f"/orgs/{login}/teams"):
            for team in page:
                if not isinstance(team, dict) or not isinstance(team.get("id"), int):
                    continue
                parent = team.get("parent")
                row = {
                    "instance_id": connection.id,
                    "provider_id": team["id"],
                    "parent_provider_id": parent.get("id") if isinstance(parent, dict) else None,
                    "slug": str(team.get("slug") or team["id"]),
                    "name": str(team.get("name") or team.get("slug") or team["id"]),
                    "privacy": team.get("privacy")
                    if isinstance(team.get("privacy"), str)
                    else None,
                    "state": "active",
                }
                statement = pg_insert(ProviderTeam).values(row)
                db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[ProviderTeam.instance_id, ProviderTeam.provider_id],
                        set_={
                            key: statement.excluded[key]
                            for key in ("parent_provider_id", "slug", "name", "privacy")
                        }
                        | {"state": "active", "last_seen_at": func.now()},
                    )
                )
                teams.append(row)
        result.teams["listed"] = len(teams)
        result.teams["missing"] = _mark_missing(db, ProviderTeam, connection.id, started)

        listed = _members(
            client,
            db,
            connection.id,
            f"/orgs/{login}/members",
            ("organization", org_id),
            ("admin", "member"),
        )
        for team in teams:
            listed += _members(
                client,
                db,
                connection.id,
                f"/orgs/{login}/teams/{team['slug']}/members",
                ("team", team["provider_id"]),
                ("maintainer", "member"),
            )
        result.members["listed"] = listed
        result.members["missing"] = _mark_missing(db, ProviderMembership, connection.id, started)
        db.commit()
    return result
