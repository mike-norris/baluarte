"""Immutable policy versions in the ``policy`` table.

A version is never changed or deleted (a database trigger enforces this from migration 0005);
saving a changed baseline under the same ``metadata.name`` adds the next version. Saving source
identical to the latest version returns that version instead of adding a duplicate.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from baluarte.db.models import Policy
from baluarte.policy.loader import parse_policy


@dataclass(frozen=True)
class Saved:
    policy: Policy
    created: bool


def latest_version(db: Session, name: str) -> Policy | None:
    return db.scalars(
        select(Policy).where(Policy.name == name).order_by(Policy.version.desc()).limit(1)
    ).first()


def save_version(db: Session, source: str) -> Saved:
    """Validate ``source`` and store it as the next version of its policy. The caller commits.

    Raises PolicyInvalid when the document is not a valid baseline.
    """
    parsed = parse_policy(source)
    # Serialises versioning per name, so concurrent saves cannot claim the same version.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext("policy:" + parsed.name))))
    latest = latest_version(db, parsed.name)
    if latest is not None and latest.yaml_digest == parsed.digest:
        return Saved(policy=latest, created=False)
    policy = Policy(
        name=parsed.name,
        version=(latest.version if latest else 0) + 1,
        source_yaml=source,
        yaml_digest=parsed.digest,
    )
    db.add(policy)
    db.flush()
    return Saved(policy=policy, created=True)
