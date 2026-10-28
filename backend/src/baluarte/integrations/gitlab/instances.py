"""The ``gitlab_instance`` row for the configured URL (ADR-0004: one instance per deployment)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from baluarte.db.models import GitLabInstance


def instance_for(db: Session, base_url: str) -> GitLabInstance:
    """Return the instance row for ``base_url``, creating it if needed (safe under concurrency)."""
    db.execute(
        pg_insert(GitLabInstance)
        .values(base_url=base_url)
        .on_conflict_do_nothing(index_elements=[GitLabInstance.base_url])
    )
    return db.scalars(select(GitLabInstance).where(GitLabInstance.base_url == base_url)).one()
