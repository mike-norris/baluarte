"""Access-control rules end to end: discovery records the observations, evaluation stores
results backed by them."""

from pathlib import Path

import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session
from test_evaluation_store import World, listing
from test_policy_schema import ACCESS_RULES, BASELINE

from baluarte.db.models import Evaluation, GitLabGroup, GitLabInstance, Observation
from baluarte.integrations.gitlab.access import (
    ACCESS_TOKENS_SOURCE,
    APPROVALS_SOURCE,
    MEMBERS_SOURCE,
    TWO_FACTOR_SOURCE,
)
from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS, make_discover_projects
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.policy.evaluation.runner import EVALUATE, evaluate

pytestmark = pytest.mark.db

PROBES = (
    "projects.list",
    "protected_branches.read",
    "merge_request_approvals.read",
    "groups.two_factor",
)


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run(engine: Engine, fake: FakeGitLab) -> dict[str, Evaluation]:
    world = World(engine)
    world.assign(BASELINE + ACCESS_RULES)
    with Session(engine) as db:
        db.execute(
            update(GitLabInstance).values(
                capability_snapshot={
                    "status": "ok",
                    "probes": {name: {"status": "supported", "endpoint": "-"} for name in PROBES},
                }
            )
        )
        db.execute(
            update(GitLabGroup).values(
                require_two_factor_authentication=True, two_factor_grace_period=24
            )
        )
        queue.enqueue(db, DISCOVER_PROJECTS, "test", max_attempts=1)
        db.commit()
    worker = Worker(
        engine, {DISCOVER_PROJECTS: make_discover_projects(fake.transport), EVALUATE: evaluate}
    )
    assert worker.run_once()
    assert worker.run_once()
    with Session(engine) as db:
        return {e.rule_id: e for e in db.scalars(select(Evaluation).order_by(Evaluation.id))}


def access_fake() -> FakeGitLab:
    fake = listing(FakeGitLab())
    fake.json("/projects/1/protected_branches", body=[{"id": 1, "name": "main"}])
    fake.json(
        "/projects/1/approvals",
        body={
            "merge_requests_author_approval": False,
            "reset_approvals_on_push": True,
        },
    )
    fake.json(
        "/projects/1/approval_rules",
        body=[
            {
                "name": "Maintainers",
                "rule_type": "regular",
                "approvals_required": 2,
                "applies_to_all_protected_branches": True,
                "protected_branches": [],
            }
        ],
    )
    fake.json(
        "/projects/1/members/all",
        body=[
            {"id": n, "username": f"maintainer-{n}", "access_level": 40, "state": "active"}
            for n in range(6)
        ],
    )
    fake.json("/projects/1/access_tokens", body=[])
    return fake


def test_access_rules_are_stored_with_their_evidence(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    found = run(migrated_engine, access_fake())

    outcomes = {rule_id: (e.outcome, e.source) for rule_id, e in found.items()}
    assert outcomes == {
        "project-visibility": ("fail", "gitlab_project.visibility"),
        "default-branch-protected": ("pass", "GET /projects/:id/protected_branches"),
        "approvals": ("pass", APPROVALS_SOURCE),
        "privileged": ("fail", MEMBERS_SOURCE),
        "two-factor": ("pass", TWO_FACTOR_SOURCE),
        "tokens": ("pass", ACCESS_TOKENS_SOURCE),
    }
    with Session(migrated_engine) as db:
        for rule_id in ("approvals", "privileged", "two-factor", "tokens"):
            evaluation = found[rule_id]
            assert evaluation.observation_id is not None
            linked = db.get_one(Observation, evaluation.observation_id)
            assert linked.source == evaluation.source
            assert linked.payload == evaluation.observed
    assert found["privileged"].reason == (
        "6 members hold Maintainer or Owner access; the policy allows at most 5."
    )


def test_an_unreadable_source_is_unknown_and_linked(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = access_fake()
    fake.json("/projects/1/access_tokens", 403, {"message": "403 Forbidden"})

    found = run(migrated_engine, fake)

    tokens = found["tokens"]
    assert (tokens.outcome, tokens.unknown_reasons) == ("unknown", ["value_unknown"])
    assert tokens.reason.startswith(
        "The access tokens of project platform/api could not be read (forbidden)"
    )
    assert tokens.observation_id is not None
