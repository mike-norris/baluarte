"""Workspaces and the connections in them (ADR-0013, roadmap M11).

A workspace is the isolation boundary. The ``default`` workspace (migration ``0029``) holds the
existing data and the primary connection; others are created on the Workspaces page (M11.4).

A request acts in one workspace and, within it, on one connection. ``baluarte.auth.dependencies``
resolves them from the session and the ``X-Baluarte-Workspace`` and ``X-Baluarte-Connection``
headers and records them on the request's database session with ``use``. Everything that needs
them reads them back from that session:

- ``current_workspace_id`` for records that belong to the workspace, queries that list them, and
  audit events;
- ``current_connection`` for the connection a page shows.

Without a request (a job, the CLI) the default workspace and the primary connection apply, as
before, unless the code passes them explicitly.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import Engine, Select, event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from baluarte.auth.roles import Role, roles_from_claim
from baluarte.db.base import Base
from baluarte.db.models import (
    AuditEvent,
    CollectionJob,
    Finding,
    GitLabInstance,
    GitLabProject,
    Notification,
    NotificationDelivery,
    Tag,
    TagAssignment,
    Workspace,
)
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.settings import ConfigurationError

DEFAULT_SLUG = "default"
# Keys on ``Session.info`` for the request's workspace and connection.
WORKSPACE = "baluarte_workspace_id"
CONNECTION = "baluarte_connection_id"
DEFAULT = "baluarte_default_workspace_id"
# The job payload key naming a job's connection (``baluarte.connections.CONNECTION``).
CONNECTION_PAYLOAD = "connection_id"


def use(db: Session, workspace_id: int, connection_id: int | None = None) -> None:
    """Make ``db`` act in a workspace and, optionally, on one of its connections."""
    db.info[DEFAULT] = default_workspace_id(db)
    db.info[WORKSPACE] = workspace_id
    if connection_id is None:
        db.info.pop(CONNECTION, None)
    else:
        db.info[CONNECTION] = connection_id


def request_workspace_id(db: Session) -> int | None:
    """The workspace ``use`` set on this session, or None outside a request."""
    value = db.info.get(WORKSPACE)
    return value if isinstance(value, int) else None


def default_workspace_id(db: Session) -> int:
    """The default workspace's ID, creating it if it is missing (safe under concurrency)."""
    existing = db.scalars(select(Workspace.id).where(Workspace.slug == DEFAULT_SLUG)).first()
    if existing is not None:
        return existing
    db.execute(
        pg_insert(Workspace)
        .values(slug=DEFAULT_SLUG, name="Default")
        .on_conflict_do_nothing(index_elements=[Workspace.slug])
    )
    return db.scalars(select(Workspace.id).where(Workspace.slug == DEFAULT_SLUG)).one()


def configured_settings() -> GitLabSettings | None:
    """The effective GitLab settings, or None when GitLab is not configured or misconfigured."""
    try:
        return load_gitlab_settings()
    except ConfigurationError:
        return None


def connection_for_url(db: Session, base_url: str) -> GitLabInstance | None:
    """The connection for ``base_url``, without creating it."""
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == base_url)).first()


def current_connection(
    db: Session, settings: GitLabSettings | None = None
) -> GitLabInstance | None:
    """The connection a request works on, or None when its workspace has none yet.

    The connection the request chose (``X-Baluarte-Connection``) when it is in the workspace;
    otherwise the primary connection when it is in the request's workspace; otherwise the
    workspace's oldest connection that is not removed. Outside a request, the primary connection.
    Pass ``settings`` when the caller already loaded the primary connection's settings.
    """
    workspace_id = request_workspace_id(db)
    chosen = db.info.get(CONNECTION)
    if isinstance(chosen, int):
        row = db.get(GitLabInstance, chosen)
        if (
            row is not None
            and row.removed_at is None
            and (workspace_id is None or row.workspace_id == workspace_id)
        ):
            return row
        return None
    settings = settings if settings is not None else configured_settings()
    primary = connection_for_url(db, settings.url) if settings is not None else None
    if workspace_id is None or (primary is not None and primary.workspace_id == workspace_id):
        return primary
    return db.scalars(
        select(GitLabInstance)
        .where(GitLabInstance.workspace_id == workspace_id, GitLabInstance.removed_at.is_(None))
        .order_by(GitLabInstance.id)
        .limit(1)
    ).first()


def current_settings(db: Session) -> GitLabSettings | None:
    """The settings of the connection a request works on, for reads that need its URL and schedule.

    The primary connection's settings when that is the connection (or, in the default workspace,
    before it is first collected); an added connection's settings without its token, which reads
    never need; None when the workspace has no connection.
    """
    primary = configured_settings()
    connection = current_connection(db, primary)
    if connection is None:
        workspace_id = request_workspace_id(db)
        default_id = db.info.get(DEFAULT)
        if primary is not None and (workspace_id is None or workspace_id == default_id):
            return primary
        return None
    if primary is not None and connection.base_url == primary.url:
        return primary
    return GitLabSettings(
        url=connection.base_url,
        token="",
        timeout_seconds=connection.timeout_seconds,
        sync_interval_minutes=connection.sync_interval_minutes,
        source="database",
    )


def current_connection_id(db: Session, settings: GitLabSettings | None = None) -> int | None:
    connection = current_connection(db, settings)
    return connection.id if connection is not None else None


def workspace_of(db: Session, connection_id: int) -> int:
    """The workspace a connection belongs to."""
    return db.scalars(
        select(GitLabInstance.workspace_id).where(GitLabInstance.id == connection_id)
    ).one()


def current_workspace_id(db: Session) -> int:
    """The workspace a request works in; the default workspace outside a request."""
    workspace_id = request_workspace_id(db)
    return workspace_id if workspace_id is not None else default_workspace_id(db)


# Roles per workspace (M11.4)


def group_roles_of(value: Mapping[str, object] | None) -> dict[Role, frozenset[str]]:
    """A workspace's stored ``group_roles`` as a role mapping; unknown roles are ignored."""
    mapping: dict[Role, frozenset[str]] = {}
    for role in Role:
        groups = (value or {}).get(role.value)
        if isinstance(groups, list):
            mapping[role] = frozenset(group for group in groups if isinstance(group, str))
    return mapping


def roles_by_workspace(
    db: Session, claim: object, default_mapping: Mapping[Role, frozenset[str]]
) -> dict[int, frozenset[Role]]:
    """The roles a groups claim grants in each workspace; workspaces granting none are left out.

    The default workspace uses the sign-in settings' mapping; every other workspace its own.
    """
    default_id = default_workspace_id(db)
    granted: dict[int, frozenset[Role]] = {}
    for workspace in db.scalars(select(Workspace).order_by(Workspace.id)):
        mapping = (
            default_mapping if workspace.id == default_id else group_roles_of(workspace.group_roles)
        )
        roles = roles_from_claim(claim, mapping)
        if roles:
            granted[workspace.id] = roles
    return granted


def stored_roles(
    value: Mapping[str, object] | None, fallback: Iterable[str], default_id: int
) -> dict[int, frozenset[Role]]:
    """A session's ``workspace_roles``; a session without them holds its roles in the default."""
    known = {role.value for role in Role}
    parsed: dict[int, frozenset[Role]] = {}
    for key, roles in (value or {}).items():
        if key.isdigit() and isinstance(roles, list):
            held = frozenset(Role(role) for role in roles if role in known)
            if held:
                parsed[int(key)] = held
    if not parsed:
        held = frozenset(Role(role) for role in fallback if role in known)
        if held:
            parsed[default_id] = held
    return parsed


# Isolation (M11.4)
#
# Every ORM query on a session that ``use`` put in a workspace sees only that workspace's rows:
# tables with ``workspace_id`` by that column, tables with ``instance_id`` through their
# connection, and findings, tag assignments, notification deliveries and jobs through what they
# belong to. Audit events without a workspace (before migration 0032, sign-in, deployment
# settings) belong to the default workspace. Inserts are unaffected: they name their workspace.
# Code that must see across workspaces in a request (a URL already connected elsewhere) passes
# the ``ALL_WORKSPACES`` execution option. Jobs and the CLI open sessions without ``use``.

ALL_WORKSPACES = "baluarte_all_workspaces"


def _in_workspace(workspace_id: int) -> Select[Any]:
    return select(GitLabInstance.id).where(GitLabInstance.workspace_id == workspace_id)


def _criteria(workspace_id: int, default_id: int) -> list[Any]:
    options: list[Any] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        columns = mapper.columns
        if cls is AuditEvent:
            continue
        if "workspace_id" in columns:
            options.append(
                with_loader_criteria(
                    cls, lambda c: c.workspace_id == workspace_id, include_aliases=True
                )
            )
        elif "instance_id" in columns:
            options.append(
                with_loader_criteria(
                    cls,
                    lambda c: c.instance_id.in_(_in_workspace(workspace_id)),
                    include_aliases=True,
                )
            )
    projects = select(GitLabProject.id).where(
        GitLabProject.instance_id.in_(_in_workspace(workspace_id))
    )
    options += [
        with_loader_criteria(Finding, lambda c: c.project_id.in_(projects), include_aliases=True),
        with_loader_criteria(
            TagAssignment,
            lambda c: c.tag_id.in_(select(Tag.id).where(Tag.workspace_id == workspace_id)),
            include_aliases=True,
        ),
        with_loader_criteria(
            NotificationDelivery,
            lambda c: c.notification_id.in_(
                select(Notification.id).where(Notification.workspace_id == workspace_id)
            ),
            include_aliases=True,
        ),
        with_loader_criteria(
            AuditEvent,
            lambda c: (
                (c.workspace_id == workspace_id)
                | (c.workspace_id.is_(None) & (workspace_id == default_id))
            ),
            include_aliases=True,
        ),
        with_loader_criteria(
            CollectionJob,
            # The key is written out, not CONNECTION_PAYLOAD: SQLAlchemy binds a global read in
            # this lambda as a parameter, and on Python 3.15 typed it JSONB (``jsonb ->> jsonb``).
            lambda c: (
                (c.payload["connection_id"].as_integer().in_(_in_workspace(workspace_id)))
                | (~c.payload.has_key("connection_id") & (workspace_id == default_id))
            ),
            include_aliases=True,
        ),
    ]
    return options


@event.listens_for(Session, "do_orm_execute")
def _scope_to_workspace(state: ORMExecuteState) -> None:
    workspace_id = request_workspace_id(state.session)
    if workspace_id is None or state.execution_options.get(ALL_WORKSPACES):
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    default_id = state.session.info.get(DEFAULT)
    if not isinstance(default_id, int):
        return
    state.statement = state.statement.options(*_criteria(workspace_id, default_id))


def request_scope(db: Session) -> dict[str, Any]:
    """The workspace and connection ``use`` put on ``db``, to open another session in them."""
    return {key: db.info[key] for key in (WORKSPACE, DEFAULT, CONNECTION) if key in db.info}


def scoped_session(engine: Engine, scope: Mapping[str, Any] | None) -> Session:
    """A new session acting in the same workspace and connection as the request it came from."""
    return Session(engine, info=dict(scope or {}))
