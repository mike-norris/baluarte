"""M13 acceptance: one workspace with a GitLab and a GitHub connection, evaluated by one baseline,
with each provider's evidence kept (roadmap M13.5).

GitLab's inventory is recorded directly (``test_evaluation_store.World``); GitHub is scripted
(``test_github_rules.organization``). Nothing here reaches a real GitLab or github.com.
"""

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_connections import environment as environment  # the fixture
from test_evaluation_store import VISIBILITY_ONLY, World
from test_github import add, collect
from test_github_rules import organization

from baluarte.db.models import Evaluation, Finding, GitLabInstance, GitLabProject, Observation
from baluarte.jobs.worker import Worker
from baluarte.policy.assignments import Target, assign
from baluarte.policy.evaluation.runner import EVALUATE, evaluate
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db


def test_one_baseline_judges_gitlab_and_github_in_one_workspace(
    migrated_engine: Engine, environment: None
) -> None:
    gitlab = World(migrated_engine)
    private = gitlab.project(1, "private")
    public = gitlab.project(2, "public")
    fake = organization()
    github_id = int(add(migrated_engine).json()["id"])

    with Session(migrated_engine) as db:
        connections = {row.id: row for row in db.scalars(select(GitLabInstance)).all()}
        assert connections[github_id].workspace_id == connections[gitlab.instance_id].workspace_id
        # One baseline, one version, assigned to both connections.
        policy = save_version(db, VISIBILITY_ONLY).policy
        policy_id = policy.id
        for row in connections.values():
            assign(
                db,
                row.id,
                policy,
                Target(target_type="instance", target_id=row.id, path=row.base_url, state=None),
            )
        db.commit()

    collect(migrated_engine, fake, github_id)
    assert Worker(migrated_engine, {EVALUATE: evaluate}).run_once()
    gitlab.run()

    with Session(migrated_engine) as db:
        rows = db.execute(
            select(
                GitLabInstance.provider, GitLabProject.path_with_namespace, Evaluation, Observation
            )
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .join(GitLabInstance, GitLabInstance.id == Evaluation.instance_id)
            .outerjoin(Observation, Observation.id == Evaluation.observation_id)
        ).all()
        opened = {
            row.project_id for row in db.scalars(select(Finding).where(Finding.state == "open"))
        }
    results = {(provider, path): (e, o) for provider, path, e, o in rows}
    assert {key: e.outcome for key, (e, _) in results.items()} == {
        ("gitlab", "platform/p1"): "pass",
        ("gitlab", "platform/p2"): "fail",
        ("github", "acme/api"): "pass",
        ("github", "acme/web"): "fail",
        ("github", "acme/legacy"): "pass",
    }
    # The same baseline and rule judged both providers.
    assert {(e.policy_id, e.rule_id) for e, _ in results.values()} == {
        (policy_id, "project-visibility")
    }
    # Each result keeps its own provider's evidence.
    sources = {provider: o.source for (provider, _), (_, o) in results.items() if o is not None}
    assert sources["gitlab"] == "GET /projects"
    assert sources["github"].startswith("GET /orgs/")
    for (provider, _), (_, observation) in results.items():
        assert observation is not None
        assert observation.instance_id == (
            github_id if provider == "github" else gitlab.instance_id
        )
    web = next(e.project_id for (provider, path), (e, _) in results.items() if path == "acme/web")
    assert opened == {public, web}
    assert private not in opened
