# ruff: noqa: F811 (pytest fixtures imported from other test modules)
"""Automatic drift remediation (M14.5): Baluarte proposes fixes, people approve them."""

from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_evaluation_access import GOOD_SETTINGS, approval_rule
from test_evaluation_project import fresh, instance_id  # noqa: F401
from test_template_change_sets import API, SITE
from test_template_drift import (  # noqa: F401
    client,
    gitlab_env,
    observe_approvals,
    user,
    world,
)

from baluarte.auth.roles import Role
from baluarte.db.models import (
    AuditEvent,
    ConfigurationTemplate,
    GitLabInstance,
    TemplateAssignment,
    TemplateChangeSet,
)
from baluarte.policy.assignments import Target
from baluarte.policy.evaluation import runner
from baluarte.templates import auto
from baluarte.templates.store import assign

pytestmark = pytest.mark.db

SETS = "/api/v1/template-change-sets"


def auto_on(engine: Engine, connection: int) -> dict[str, Any]:
    """Assign the template to the connection again, with automatic remediation."""
    response = client(engine, user(engine, "pat"), Role.ANALYST).post(
        "/api/v1/template-assignments",
        json={"name": "service", "version": 1, "target_type": "instance", "auto_remediate": True},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    assert body["auto_remediate"] is True
    return body


def propose(engine: Engine, connection: int) -> int | None:
    with Session(engine) as db:
        row = auto.propose(db, db.get_one(GitLabInstance, connection))
        db.commit()
        return row.id if row is not None else None


def test_nothing_is_proposed_without_automatic_remediation(
    migrated_engine: Engine, world: int
) -> None:
    assert propose(migrated_engine, world) is None
    listed = client(migrated_engine, user(migrated_engine, "ana"), Role.AUDITOR).get(
        "/api/v1/template-assignments"
    )
    assert [a["auto_remediate"] for a in listed.json()["assignments"]] == [False]


def test_drift_is_proposed_by_the_system_and_approved_by_people(
    migrated_engine: Engine, world: int
) -> None:
    assignment = auto_on(migrated_engine, world)
    proposed = propose(migrated_engine, world)
    assert proposed is not None

    reader = client(migrated_engine, user(migrated_engine, "ana"), Role.AUDITOR)
    body = reader.get(f"{SETS}/{proposed}").json()
    assert (body["state"], body["created_by"], body["created_by_system"]) == (
        "proposed",
        None,
        True,
    )
    assert body["source_kind"] == "effective"
    assert body["document"]["source"]["assignments"] == [assignment["id"]]
    # Only the drifting project; the aligned one is left alone.
    assert [t["project"]["path"] for t in body["document"]["targets"]] == [SITE]

    # While it is open, nothing more is proposed.
    assert propose(migrated_engine, world) is None

    # Anyone with changes:approve decides it; a sensitive set still needs two people.
    bea, cy = user(migrated_engine, "bea"), user(migrated_engine, "cy")
    first = client(migrated_engine, bea, Role.ADMIN).post(
        f"{SETS}/{proposed}/approve", json={"digest": body["digest"]}
    )
    assert first.status_code == 200, first.text
    assert body["sensitive"] is True and first.json()["state"] == "proposed"
    again = client(migrated_engine, bea, Role.ADMIN).post(
        f"{SETS}/{proposed}/approve", json={"digest": body["digest"]}
    )
    assert again.status_code == 403
    second = client(migrated_engine, cy, Role.ADMIN).post(
        f"{SETS}/{proposed}/approve", json={"digest": body["digest"]}
    )
    assert second.json()["state"] == "approved"
    # An approved set is still open: nothing more is proposed, and nothing was applied.
    assert propose(migrated_engine, world) is None
    assert second.json()["items"] == []

    with Session(migrated_engine) as db:
        created = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "template_change_set.created")
        ).one()
    assert created.actor == auto.SYSTEM_ACTOR


def test_a_rejected_proposal_is_not_repeated_until_the_drift_changes(
    migrated_engine: Engine, world: int
) -> None:
    auto_on(migrated_engine, world)
    first = propose(migrated_engine, world)
    rejected = client(migrated_engine, user(migrated_engine, "bea"), Role.ADMIN).post(
        f"{SETS}/{first}/reject", json={"note": "The site stays public for now."}
    )
    assert rejected.json()["state"] == "rejected"
    assert propose(migrated_engine, world) is None

    # Someone forbids author approval on the site: the drift is different, so it is proposed.
    observe_approvals(
        migrated_engine,
        world,
        101,
        {
            "branch": "main",
            "branch_protected": False,
            "settings": GOOD_SETTINGS,
            "rules": [approval_rule(3)],
        },
    )
    assert propose(migrated_engine, world) not in (None, first)


def test_only_settings_decided_by_the_automatic_assignment(
    migrated_engine: Engine, world: int
) -> None:
    auto_on(migrated_engine, world)
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, world)
        template = db.scalars(select(ConfigurationTemplate)).one()
        # The site's own assignment, without automatic remediation, decides all its settings.
        site = Target(target_type="project", target_id=101, path=SITE, state="active")
        assign(db, instance, template, site, None, None, "test")
        db.commit()
        auto_ids = set(
            db.scalars(
                select(TemplateAssignment.id).where(TemplateAssignment.auto_remediate.is_(True))
            )
        )
    assert len(auto_ids) == 1
    assert propose(migrated_engine, world) is None


def test_evaluation_proposes_and_a_failure_never_fails_it(
    migrated_engine: Engine, world: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    auto_on(migrated_engine, world)
    runner._propose_drift_fixes(migrated_engine, world)
    with Session(migrated_engine) as db:
        sets = db.scalars(select(TemplateChangeSet)).all()
    assert [(s.created_by_system, s.state) for s in sets] == [(True, "proposed")]
    assert API not in {t["project"]["path"] for t in sets[0].document["targets"]}

    def broken(*_: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(auto, "propose", broken)
    runner._propose_drift_fixes(migrated_engine, world)  # logged, not raised
