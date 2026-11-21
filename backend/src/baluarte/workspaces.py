"""Workspaces and the connections in them (ADR-0013, roadmap M11).

A workspace is the isolation boundary. Until several workspaces are offered (M11.4), every
connection belongs to the ``default`` workspace, created by migration ``0029``.

``current_connection`` is how the API finds the connection a request works on: today, the GitLab
connection for the configured URL. Collection jobs, which already hold the settings they run
with, look up their connection by URL in ``baluarte.integrations.gitlab.instances``. Choosing among
several connections (M11.3) changes these two places.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance, Workspace
from baluarte.integrations.gitlab.settings import GitLabSettings, load_gitlab_settings
from baluarte.settings import ConfigurationError

DEFAULT_SLUG = "default"


def default_workspace_id(db: Session) -> int:
    """The default workspace's ID, creating it if it is missing (safe under concurrency)."""
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
    """The connection for the configured GitLab, or None when there is none yet.

    Pass ``settings`` when the caller already loaded them.
    """
    settings = settings if settings is not None else configured_settings()
    if settings is None:
        return None
    return connection_for_url(db, settings.url)


def current_connection_id(db: Session, settings: GitLabSettings | None = None) -> int | None:
    connection = current_connection(db, settings)
    return connection.id if connection is not None else None


def workspace_of(db: Session, connection_id: int) -> int:
    """The workspace a connection belongs to."""
    return db.scalars(
        select(GitLabInstance.workspace_id).where(GitLabInstance.id == connection_id)
    ).one()


def current_workspace_id(db: Session) -> int:
    """The workspace a request or job works in, for records that carry no connection.

    Always the default workspace until a deployment can hold several (M11.4), when it comes from
    the request or job.
    """
    return default_workspace_id(db)
