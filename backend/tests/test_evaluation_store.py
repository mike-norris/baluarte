"""Storing evaluation results, following findings and running the ``policy.evaluate`` job."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, paged, project
from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_project import observe_protection, protection, set_probe
from test_policy_schema import BASELINE

from baluarte.db.models import (
    CollectionJob,
    CollectionRun,
    Evaluation,
    EvaluationRun,
    Finding,
    GitLabGroup,
    GitLabInstance,
    GitLabProject,
    Observation,
    PolicyAssignment,
)
from baluarte.evidence.observations import Observed, record
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import DISCOVER_PROJECTS, make_discover_projects
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.policy.assignments import Target, assign, resolve_project
from baluarte.policy.evaluation import runner
from baluarte.policy.evaluation.project import collection_contexts, evaluate_project
from baluarte.policy.evaluation.runner import (
    EVALUATE,
    evaluate,
    evaluate_all,
    request_evaluation,
)
from baluarte.policy.evaluation.store import Tally, store_project
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

SETTINGS = GitLabSettings(url=URL, token=TOKEN)
# The baseline without its default-branch rule, so each project has one result.
VISIBILITY_ONLY = BASELINE.replace(
    """    - id: default-branch-protected
      type: gitlab.branch.default_protected
      required: true
""",
    "",
)
NEXT_VERSION = VISIBILITY_ONLY.replace("allowed: [private, internal]", "allowed: [private]")
HIGH = VISIBILITY_ONLY.replace(
    "allowed: [private, internal]", "allowed: [private, internal]\n      severity: high"
)


class World:
    """An instance with a fresh projects collection and the baseline assigned to it."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        with Session(engine) as db:
            instance = instance_for(db, URL)
            self.instance_id = instance.id
            db.add(
                GitLabGroup(
                    instance_id=instance.id, gitlab_id=10, full_path="platform", name="platform"
                )
            )
            db.commit()
        self.collection("succeeded")

    def collection(self, status: str, age: timedelta = timedelta(minutes=5)) -> None:
        with Session(self.engine) as db:
            finished = func.now() - age
            db.execute(
                insert(CollectionRun).values(
                    instance_id=self.instance_id,
                    kind="projects",
                    status=status,
                    started_at=finished,
                    finished_at=finished,
                )
            )
            db.commit()

    def save(self, source: str = VISIBILITY_ONLY) -> int:
        with Session(self.engine) as db:
            policy_id = save_version(db, source).policy.id
            db.commit()
            return policy_id

    def assign(self, source: str = VISIBILITY_ONLY, target: Target | None = None) -> None:
        with Session(self.engine) as db:
            policy = save_version(db, source).policy
            target = target or Target(
                target_type="instance", target_id=self.instance_id, path=URL, state=None
            )
            assign(db, self.instance_id, policy, target)
            db.commit()

    def project(
        self,
        gitlab_id: int,
        visibility: str | None,
        *,
        observe: bool = True,
        state: str = "active",
        namespace: int = 10,
        default_branch: str | None = None,
    ) -> int:
        with Session(self.engine) as db:
            row = GitLabProject(
                instance_id=self.instance_id,
                gitlab_id=gitlab_id,
                namespace_gitlab_id=namespace,
                namespace_kind="group",
                path_with_namespace=f"platform/p{gitlab_id}",
                name=f"p{gitlab_id}",
                visibility=visibility,
                state=state,
                default_branch=default_branch,
            )
            db.add(row)
            db.commit()
            project_id = row.id
        if observe:
            self.observe(project_id, visibility)
        return project_id

    def observe(self, project_id: int, visibility: str | None, *, update_row: bool = True) -> None:
        with Session(self.engine) as db:
            row = db.get_one(GitLabProject, project_id)
            if update_row:
                row.visibility = visibility
            payload = {"gitlab_id": row.gitlab_id, "visibility": visibility}
            record(
                db,
                self.instance_id,
                "project_id",
                [
                    Observed(
                        project_id, "GET /projects", f"/projects/{row.gitlab_id}", "ok", payload
                    )
                ],
            )
            db.commit()

    def run(self) -> EvaluationRun:
        run_id, _ = evaluate_all(self.engine, SETTINGS)
        with Session(self.engine) as db:
            return db.get_one(EvaluationRun, run_id)

    def findings(self) -> list[Finding]:
        with Session(self.engine) as db:
            return list(db.scalars(select(Finding).order_by(Finding.id)))

    def evaluations(self, project_id: int | None = None) -> list[Evaluation]:
        with Session(self.engine) as db:
            statement = select(Evaluation).order_by(Evaluation.id)
            if project_id is not None:
                statement = statement.where(Evaluation.project_id == project_id)
            return list(db.scalars(statement))


@pytest.fixture
def world(migrated_engine: Engine) -> World:
    world = World(migrated_engine)
    world.assign()
    return world


def test_a_fail_is_stored_with_its_evidence_and_opens_a_finding(world: World) -> None:
    project_id = world.project(1, "public")

    run = world.run()

    (evaluation,) = world.evaluations()
    with Session(world.engine) as db:
        observation = db.scalars(
            select(Observation).where(Observation.project_id == project_id)
        ).one()
    assert evaluation.outcome == "fail"
    assert evaluation.observation_id == observation.id
    assert evaluation.evaluation_run_id == run.id
    assert (evaluation.project_id, evaluation.instance_id) == (project_id, world.instance_id)
    assert (evaluation.policy_name, evaluation.rule_id) == (
        "default-gitlab-baseline",
        "project-visibility",
    )
    assert evaluation.rule_type == "gitlab.project.visibility"
    assert evaluation.severity == "medium"
    assert evaluation.expected == {"allowed": ["private", "internal"]}
    assert evaluation.observed == "public"
    assert evaluation.source == "gitlab_project.visibility"
    assert evaluation.unknown_reasons == []
    assert (evaluation.collection_kind, evaluation.collection_status) == ("projects", "fresh")
    assert evaluation.collection_last_success_at is not None
    assert (evaluation.capability_name, evaluation.capability_status) == (None, "not_required")
    assert evaluation.ancestry == "complete"
    (finding,) = world.findings()
    assert finding.state == "open"
    assert finding.latest_evaluation_id == evaluation.id
    assert finding.latest_outcome == "fail"
    assert finding.first_seen_at == finding.last_seen_at == evaluation.evaluated_at
    assert (finding.resolved_at, finding.resolution) == (None, None)
    assert run.status == "succeeded"
    assert run.projects == 1
    assert run.counts == {"outcomes": {"fail": 1}, "findings": {"opened": 1}}


def test_each_rule_links_the_observation_it_judged(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    world.assign(BASELINE)
    set_probe(migrated_engine, world.instance_id, "supported")
    project_id = world.project(1, "private", default_branch="main")
    observe_protection(migrated_engine, world.instance_id, 1, protection("main", False))

    run = world.run()

    with Session(migrated_engine) as db:
        sources = {
            row.id: row.source
            for row in db.scalars(select(Observation).where(Observation.project_id == project_id))
        }
    by_rule = {e.rule_id: e for e in world.evaluations()}
    assert set(by_rule) == {"project-visibility", "default-branch-protected"}
    visibility = by_rule["project-visibility"]
    branch = by_rule["default-branch-protected"]
    assert (visibility.outcome, sources[visibility.observation_id or 0]) == (
        "pass",
        "GET /projects",
    )
    assert (branch.outcome, sources[branch.observation_id or 0]) == ("fail", PROTECTION_SOURCE)
    assert branch.observed == protection("main", False)
    assert (branch.capability_name, branch.capability_status) == (
        "protected_branches.read",
        "supported",
    )
    (finding,) = world.findings()
    assert finding.rule_id == "default-branch-protected"
    assert run.counts["outcomes"] == {"pass": 1, "fail": 1}


def test_a_protection_result_must_match_its_observation(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    world.assign(BASELINE)
    set_probe(migrated_engine, world.instance_id, "supported")
    world.project(1, "private", default_branch="main")
    observe_protection(migrated_engine, world.instance_id, 1, protection("main", True))

    with Session(migrated_engine) as db:
        project_row = db.scalars(select(GitLabProject)).one()
        resolution = resolve_project(db, db.get_one(GitLabInstance, world.instance_id), project_row)
        collections = collection_contexts(db, world.instance_id, SETTINGS)
        (_, branch) = evaluate_project(db, project_row, resolution, collections)
        # The value judged differs from the latest stored protection observation.
        tampered = branch.model_copy(update={"observed": protection("main", False)})
        (stored,) = store_project(
            db, world.instance_id, project_row, resolution, [tampered], None, Tally()
        )

    assert branch.outcome == "pass"
    assert (stored.outcome, stored.unknown_reasons) == ("unknown", ["evidence_mismatch"])


def test_a_pass_opens_nothing(world: World) -> None:
    world.project(1, "private")

    run = world.run()

    assert [e.outcome for e in world.evaluations()] == ["pass"]
    assert world.findings() == []
    assert run.counts == {"outcomes": {"pass": 1}, "findings": {}}


def test_a_finding_resolves_on_pass_and_reopens_on_fail(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    (opened,) = world.findings()

    world.observe(project_id, "private")
    resolved_run = world.run()
    (resolved,) = world.findings()

    assert resolved.id == opened.id
    assert (resolved.state, resolved.resolution, resolved.latest_outcome) == (
        "resolved",
        "passed",
        "pass",
    )
    assert resolved.resolved_at == world.evaluations()[-1].evaluated_at
    assert resolved_run.counts["findings"] == {"resolved": 1}

    world.observe(project_id, "public")
    reopened_run = world.run()
    (reopened,) = world.findings()

    assert reopened.id == opened.id
    assert (reopened.state, reopened.resolution, reopened.resolved_at) == ("open", None, None)
    assert reopened.first_seen_at == opened.first_seen_at
    assert reopened.last_seen_at > opened.last_seen_at
    assert reopened_run.counts["findings"] == {"reopened": 1}
    assert len(world.evaluations()) == 3


def test_unknown_never_resolves_a_finding(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    world.observe(project_id, "private")
    world.collection("failed", timedelta(minutes=1))

    world.run()

    (finding,) = world.findings()
    latest = world.evaluations()[-1]
    assert latest.outcome == "unknown"
    assert latest.unknown_reasons == ["collection_failed"]
    assert latest.observed is None
    # Still linked to the project's latest observation, as evidence of what was known.
    assert latest.observation_id is not None
    assert (finding.state, finding.latest_outcome) == ("open", "unknown")
    assert finding.latest_evaluation_id == latest.id


def test_unknown_never_opens_a_finding(world: World) -> None:
    world.project(1, None)

    run = world.run()

    (evaluation,) = world.evaluations()
    assert (evaluation.outcome, evaluation.unknown_reasons) == ("unknown", ["value_unknown"])
    assert world.findings() == []
    assert run.counts["outcomes"] == {"unknown": 1}


def test_a_value_without_an_observation_is_unknown(world: World) -> None:
    world.project(1, "public", observe=False)

    world.run()

    (evaluation,) = world.evaluations()
    assert evaluation.outcome == "unknown"
    assert evaluation.unknown_reasons == ["evidence_missing"]
    assert evaluation.observation_id is None
    assert evaluation.observed is None
    assert "No stored observation of project platform/p1" in evaluation.reason
    assert world.findings() == []


def test_a_value_that_does_not_match_its_observation_is_unknown(world: World) -> None:
    project_id = world.project(1, "private")
    with Session(world.engine) as db:
        db.execute(update(GitLabProject).values(visibility="public"))
        db.commit()

    world.run()

    (evaluation,) = world.evaluations()
    assert evaluation.outcome == "unknown"
    assert evaluation.unknown_reasons == ["evidence_mismatch"]
    assert evaluation.observation_id is not None
    assert world.findings() == []
    assert project_id


def test_an_unsuccessful_observation_does_not_back_a_value(world: World) -> None:
    project_id = world.project(1, "public")
    with Session(world.engine) as db:
        record(
            db,
            world.instance_id,
            "project_id",
            [Observed(project_id, "GET /projects/:id", "/projects/1", "failed", error="502")],
        )
        db.commit()

    world.run()

    (evaluation,) = world.evaluations()
    assert evaluation.unknown_reasons == ["evidence_missing"]


def test_a_new_version_continues_the_same_finding(world: World) -> None:
    world.project(1, "public")
    world.run()
    (before,) = world.findings()

    world.assign(NEXT_VERSION)
    world.run()

    (after,) = world.findings()
    assert after.id == before.id
    assert after.policy_id != before.policy_id
    assert world.evaluations()[-1].policy_id == after.policy_id


def test_severity_comes_from_the_rule(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    world.assign(HIGH)
    world.project(1, "public")

    world.run()

    (finding,) = world.findings()
    assert finding.severity == "high"
    assert world.evaluations()[0].severity == "high"


def test_a_rule_that_no_longer_applies_resolves_its_finding(world: World) -> None:
    world.project(1, "public")
    world.run()
    with Session(world.engine) as db:
        db.execute(delete(PolicyAssignment))
        db.commit()

    run = world.run()

    (finding,) = world.findings()
    assert (finding.state, finding.resolution) == ("resolved", "no_longer_applies")
    assert finding.resolved_at is not None
    assert finding.latest_outcome == "fail"
    assert run.counts["findings"] == {"no_longer_applies": 1}


def test_a_gap_in_the_group_chain_does_not_resolve_a_finding(migrated_engine: Engine) -> None:
    world = World(migrated_engine)
    group = Target(target_type="group", target_id=10, path="platform", state="active")
    world.assign(target=group)
    world.project(1, "public")
    world.run()
    with Session(world.engine) as db:
        db.execute(delete(GitLabGroup))
        db.commit()

    world.run()

    # The group's assignment can no longer be followed, so its absence proves nothing.
    (finding,) = world.findings()
    assert finding.state == "open"
    assert len(world.evaluations()) == 1


def test_an_excepted_finding_is_left_to_the_exception_workflow(world: World) -> None:
    project_id = world.project(1, "public")
    world.run()
    with Session(world.engine) as db:
        db.execute(update(Finding).values(state="excepted"))
        db.commit()
    world.observe(project_id, "private")

    world.run()

    (finding,) = world.findings()
    assert (finding.state, finding.latest_outcome) == ("excepted", "pass")


def test_missing_projects_are_evaluated_only_while_they_have_an_open_finding(
    world: World,
) -> None:
    flagged = world.project(1, "public")
    world.project(2, "private", state="missing")
    world.run()
    with Session(world.engine) as db:
        db.execute(update(GitLabProject).where(GitLabProject.id == flagged).values(state="missing"))
        db.commit()

    run = world.run()

    latest = world.evaluations(flagged)[-1]
    assert (latest.outcome, latest.unknown_reasons[0]) == ("unknown", "target_missing")
    assert world.findings()[0].state == "open"
    assert run.projects == 1


def test_evaluations_cannot_be_updated(world: World) -> None:
    world.project(1, "public")
    world.run()

    with (
        Session(world.engine) as db,
        pytest.raises(DBAPIError, match="UPDATE on evaluation is not allowed"),
    ):
        db.execute(update(Evaluation).values(reason="changed"))
        db.commit()


def test_a_run_pages_through_every_project(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "PAGE_SIZE", 2)
    for gitlab_id in range(1, 6):
        world.project(gitlab_id, "public")

    run = world.run()

    assert run.projects == 5
    assert len(world.findings()) == 5


def test_running_again_adds_evaluations_but_no_findings(world: World) -> None:
    world.project(1, "public")
    world.run()
    world.run()

    assert len(world.evaluations()) == 2
    assert len(world.findings()) == 1
    assert world.findings()[0].latest_evaluation_id == world.evaluations()[-1].id


def test_a_failed_run_is_recorded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.project(1, "public")

    def broken(*_: Any) -> None:
        raise RuntimeError("database credentials: hunter2")

    monkeypatch.setattr(runner, "store_project", broken)
    with pytest.raises(RuntimeError):
        world.run()

    with Session(world.engine) as db:
        run = db.scalars(select(EvaluationRun)).one()
    assert run.status == "failed"
    assert run.error == "unexpected error: RuntimeError"
    assert run.finished_at is not None


def test_an_interrupted_run_is_closed_by_the_next(world: World) -> None:
    with Session(world.engine) as db:
        db.add(EvaluationRun(instance_id=world.instance_id))
        db.commit()

    world.run()

    with Session(world.engine) as db:
        first, second = db.scalars(select(EvaluationRun).order_by(EvaluationRun.id))
    assert (first.status, first.error) == ("failed", runner.INTERRUPTED)
    assert second.status == "succeeded"


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def discover_and_evaluate(engine: Engine, fake: FakeGitLab) -> tuple[CollectionRun, World]:
    world = World(engine)
    world.assign(BASELINE)
    set_probe(engine, world.instance_id, "supported")
    with Session(engine) as db:
        queue.enqueue(db, DISCOVER_PROJECTS, "test", max_attempts=1)
        db.commit()
    worker = Worker(
        engine, {DISCOVER_PROJECTS: make_discover_projects(fake.transport), EVALUATE: evaluate}
    )
    assert worker.run_once()
    assert worker.run_once()
    with Session(engine) as db:
        discovery_run = db.scalars(
            select(CollectionRun).where(CollectionRun.collection_job_id.is_not(None))
        ).one()
    return discovery_run, world


def listing(fake: FakeGitLab) -> FakeGitLab:
    fake.set(
        "/projects", paged("/projects", [[project(1, "platform/api", 10, visibility="public")]])
    )
    return fake


def test_project_discovery_queues_an_evaluation_that_the_worker_runs(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = listing(FakeGitLab())
    fake.json("/projects/1/protected_branches", body=[{"id": 1, "name": "main"}])

    discovery_run, world = discover_and_evaluate(migrated_engine, fake)

    with Session(migrated_engine) as db:
        jobs = {job.kind: job for job in db.scalars(select(CollectionJob))}
        run = db.scalars(select(EvaluationRun)).one()
    assert jobs[EVALUATE].idempotency_key == f"{EVALUATE}:after-run:{discovery_run.id}"
    assert jobs[EVALUATE].state == "succeeded"
    assert run.collection_job_id == jobs[EVALUATE].id
    assert discovery_run.status == "succeeded"
    assert discovery_run.counts["protection"] == {"protected": 1, "unprotected": 0, "unread": 0}
    # Discovery recorded both observations, so both results are backed by evidence.
    outcomes = {e.rule_id: (e.outcome, e.source) for e in world.evaluations()}
    assert outcomes == {
        "project-visibility": ("fail", "gitlab_project.visibility"),
        "default-branch-protected": ("pass", PROTECTION_SOURCE),
    }
    assert all(e.observation_id is not None for e in world.evaluations())
    assert [f.rule_id for f in world.findings()] == ["project-visibility"]


def test_unread_protection_makes_the_discovery_partial(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = listing(FakeGitLab())
    fake.set("/projects/1/protected_branches", httpx.Response(502))

    discovery_run, world = discover_and_evaluate(migrated_engine, fake)

    assert discovery_run.status == "partial"
    assert discovery_run.counts["protection"] == {"protected": 0, "unprotected": 0, "unread": 1}
    assert discovery_run.error == "default-branch protection of 1 projects was not read"
    branch = {e.rule_id: e for e in world.evaluations()}["default-branch-protected"]
    assert (branch.outcome, branch.unknown_reasons) == ("unknown", ["value_unknown"])
    # The failed read is the evidence linked to the unknown result.
    assert branch.observation_id is not None


def test_evaluation_without_gitlab_is_a_permanent_failure(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BALUARTE_GITLAB_URL", raising=False)
    with Session(migrated_engine) as db:
        job_id = queue.enqueue(db, EVALUATE, "test", max_attempts=3)
        db.commit()

    assert Worker(migrated_engine, {EVALUATE: evaluate}).run_once()

    with Session(migrated_engine) as db:
        job = db.get_one(CollectionJob, job_id)
    assert job.state == "failed"
    assert "not configured" in (job.error or "")


def test_requests_share_a_queued_evaluation(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        first = request_evaluation(db, "assignment")
        second = request_evaluation(db, "assignment")
        db.commit()
        jobs = list(db.scalars(select(CollectionJob)))

    assert first is not None
    assert second is None
    assert [job.kind for job in jobs] == [EVALUATE]


def test_evaluation_uses_the_database_clock(world: World) -> None:
    world.project(1, "public")
    world.run()

    with Session(world.engine) as db:
        skew = db.execute(text("SELECT now() - max(evaluated_at) FROM evaluation")).scalar_one()
    assert skew >= timedelta(0)
