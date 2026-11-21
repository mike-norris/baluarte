"""The ``gitlab_instance`` row (a connection, ADR-0013) for a configured URL."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance
from baluarte.workspaces import default_workspace_id


def instance_for(db: Session, base_url: str) -> GitLabInstance:
    """Return the connection for ``base_url``, creating it in the default workspace if needed
    (safe under concurrency)."""
    db.execute(
        pg_insert(GitLabInstance)
        .values(base_url=base_url, workspace_id=default_workspace_id(db))
        .on_conflict_do_nothing(index_elements=[GitLabInstance.base_url])
    )
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == base_url)).one()


def instance_id_for(db: Session, base_url: str) -> int | None:
    """The connection's ID for ``base_url`` without creating it (for read-only requests)."""
    return db.scalars(select(GitLabInstance.id).where(GitLabInstance.base_url == base_url)).first()
