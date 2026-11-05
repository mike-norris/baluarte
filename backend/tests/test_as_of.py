"""Monitoring scope and the control mapping as they stood at a past moment."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from test_evaluation_store import World

from baluarte.controls import changes as mapping_changes
from baluarte.controls.posture import effective
from baluarte.db.models import GitLabProject
from baluarte.policy.changes import Actor
from baluarte.scope import exclusions
from baluarte.scope.exclusions import Target, coverage

pytestmark = pytest.mark.db

ADMIN = Actor(user_id=1, actor="user:1")
WHY = "Sandbox project kept out of scope for this period."


def now(engine: Engine) -> datetime:
    with Session(engine) as db:
        moment: datetime = db.execute(select(func.clock_timestamp())).scalar_one()
        return moment


def test_monitoring_scope_as_of_a_moment(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    project_id = world.project(1, "private")
    before = now(migrated_engine)
    with Session(migrated_engine) as db:
        [row] = exclusions.propose(db, world.instance_id, [Target("project", 1)], False, WHY, ADMIN)
        db.commit()
        exclusion_id = row.id
    during = now(migrated_engine)
    with Session(migrated_engine) as db:
        exclusions.end(db, world.instance_id, [exclusion_id], WHY, ADMIN)
        db.commit()

    with Session(migrated_engine) as db:
        project = db.get_one(GitLabProject, project_id)

        def excluded(as_of: datetime | None) -> bool:
            return coverage(db, world.instance_id, as_of).of(project) is not None

        assert not excluded(before)
        assert excluded(during)
        assert not excluded(None)
        assert not excluded(now(migrated_engine))


def test_the_control_mapping_as_of_a_moment(migrated_engine: Engine) -> None:
    reviewed = "gitlab.change.reviewed"
    before = now(migrated_engine)
    with Session(migrated_engine) as db:
        unmapped = mapping_changes.propose(
            db,
            mapping_changes.Proposal("soc2", "CC8.1", "unmap", WHY, rule_type=reviewed),
            ADMIN,
        )
        db.commit()
        unmap_id = unmapped.id
    during = now(migrated_engine)
    with Session(migrated_engine) as db:
        mapping_changes.propose(
            db,
            mapping_changes.Proposal("soc2", "CC8.1", "withdraw", WHY, supersedes_id=unmap_id),
            ADMIN,
        )
        db.commit()

    with Session(migrated_engine) as db:

        def mapped(as_of: datetime | None) -> bool:
            return reviewed in effective(db, as_of).rule_types("soc2", "CC8.1")

        assert mapped(before)
        assert not mapped(during)
        assert mapped(None)
        assert mapped(now(migrated_engine) + timedelta(seconds=1))
