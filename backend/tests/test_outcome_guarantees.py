"""End to end: unknown, unsupported and stale state is never reported as compliant (BAL-022).

Each test drives the real job handlers through the worker against a scripted GitLab and a real
database: capability detection, group discovery, project discovery (with default-branch
protection reads) and ``policy.evaluate``. It then checks the stored evaluations and findings,
the run counts and the API. Time is moved by shifting stored timestamps back against the
database clock, never by sleeping.

Every scenario starts from the same healthy collection: ``platform/api`` (1) is private with a
protected ``main`` and passes both rules; ``platform/site`` (2) is public with an unprotected
``main`` and fails both, so it has two open findings.
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL, FakeGitLab, group, healthy_routes, paged, project
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session
from test_findings_api import get
from test_policy_schema import BASELINE

from baluarte.db.models import (
    CollectionRun,
    Evaluation,
    EvaluationRun,
    Finding,
    GitLabProject,
    Observation,
)
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import (
    DETECT_CAPABILITIES,
    DISCOVER_GROUPS,
    DISCOVER_PROJECTS,
    SCHEDULED_KINDS,
    make_detect_capabilities,
    make_discover_groups,
    make_discover_projects,
)
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.policy.assignments import Target, assign
from baluarte.policy.evaluation import runner
from baluarte.policy.evaluation.runner import EVALUATE, evaluate
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

VISIBILITY = "project-visibility"
BRANCH = "default-branch-protected"
# Longer than the stale threshold for the default 360-minute schedule (twice the interval).
PAST_STALE = timedelta(hours=13)

API = project(1, "platform/api", 10)
SITE = project(2, "platform/site", 10, visibility="public")
PROTECTED = [{"id": 1, "name": "main"}]


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES", raising=False)


class Pipeline:
    """A scripted GitLab, the real handlers and helpers to read what they stored."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.fake = FakeGitLab(healthy_routes())
        self.batch = 0
        with Session(engine) as db:
            self.instance_id = instance_for(db, URL).id
            policy = save_version(db, BASELINE).policy
            root = Target(target_type="instance", target_id=self.instance_id, path=URL, state=None)
            assign(db, self.instance_id, policy, root)
            db.commit()

    def gitlab(
        self,
        projects: list[dict[str, Any]],
        groups: list[dict[str, Any]] | None = None,
        protection: dict[int, httpx.Response] | None = None,
    ) -> None:
        """What GitLab lists now; each project's protection defaults to a protected ``main``."""
        self.fake.set("/groups", paged("/groups", [groups or [group(10, "platform")]]))
        self.fake.set("/projects", paged("/projects", [projects]))
        for listed in projects:
            gitlab_id = listed["id"]
            reply = (protection or {}).get(gitlab_id)
            if reply is None:
                body = PROTECTED if gitlab_id != SITE["id"] else []
                reply = httpx.Response(200, json=body)
            self.fake.set(f"/projects/{gitlab_id}/protected_branches", reply)

    def run(self, *kinds: str) -> None:
        """Queue the jobs in order, then let the worker run everything they queue."""
        self.batch += 1
        with Session(self.engine) as db:
            for kind in kinds:
                queue.enqueue(db, kind, f"{kind}:test:{self.batch}", max_attempts=1)
            db.commit()
        transport = self.fake.transport
        worker = Worker(
            self.engine,
            {
                DETECT_CAPABILITIES: make_detect_capabilities(transport),
                DISCOVER_GROUPS: make_discover_groups(transport),
                DISCOVER_PROJECTS: make_discover_projects(transport),
                EVALUATE: evaluate,
            },
        )
        while worker.run_once():
            pass

    def collect(self) -> None:
        """A full scheduled collection; project discovery queues the evaluation."""
        self.run(*SCHEDULED_KINDS)

    def evaluate(self) -> None:
        self.run(EVALUATE)

    def latest(self, gitlab_id: int) -> dict[str, tuple[str, list[str]]]:
        """The newest result of each rule for a project: (outcome, unknown reasons)."""
        with Session(self.engine) as db:
            rows = db.scalars(
                select(Evaluation)
                .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
                .where(GitLabProject.gitlab_id == gitlab_id)
                .order_by(Evaluation.id)
            ).all()
        return {row.rule_id: (row.outcome, list(row.unknown_reasons)) for row in rows}

    def findings(self) -> dict[tuple[int, str], tuple[str, str]]:
        """(project, rule) to (state, latest outcome)."""
        with Session(self.engine) as db:
            rows = db.execute(
                select(
                    GitLabProject.gitlab_id, Finding.rule_id, Finding.state, Finding.latest_outcome
                ).join(GitLabProject, GitLabProject.id == Finding.project_id)
            ).all()
        return {(row[0], row[1]): (row[2], row[3]) for row in rows}

    def finding_ids(self) -> dict[tuple[int, str], int]:
        with Session(self.engine) as db:
            rows = db.execute(
                select(GitLabProject.gitlab_id, Finding.rule_id, Finding.id).join(
                    GitLabProject, GitLabProject.id == Finding.project_id
                )
            ).all()
        return {(row[0], row[1]): row[2] for row in rows}

    def last_evaluation_run(self) -> EvaluationRun:
        with Session(self.engine) as db:
            return db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc())).first()  # type: ignore[return-value]

    def last_collection(self, kind: str) -> CollectionRun:
        with Session(self.engine) as db:
            return db.scalars(  # type: ignore[return-value]
                select(CollectionRun)
                .where(CollectionRun.kind == kind)
                .order_by(CollectionRun.id.desc())
            ).first()

    def age(self, model: type[CollectionRun] | type[EvaluationRun]) -> None:
        """Move a run table's history back, as if ``PAST_STALE`` had passed."""
        with Session(self.engine) as db:
            db.execute(
                update(model).values(
                    started_at=model.started_at - PAST_STALE,
                    finished_at=model.finished_at - PAST_STALE,
                )
            )
            db.commit()

    def tamper(self, gitlab_id: int, source: str, path: str, value: str) -> None:
        """Rewrite the latest observation's payload without its digest (a privileged change)."""
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            connection.execute(
                text(
                    "UPDATE observation SET payload = jsonb_set(payload, CAST(:path AS text[]), "
                    "CAST(:value AS jsonb)) WHERE id = (SELECT o.id FROM observation o "
                    "JOIN gitlab_project p ON p.id = o.project_id WHERE p.gitlab_id = :gitlab_id "
                    "AND o.source = :source ORDER BY o.collected_at DESC, o.id DESC LIMIT 1)"
                ),
                {"path": path, "value": value, "gitlab_id": gitlab_id, "source": source},
            )


@pytest.fixture
def pipeline(migrated_engine: Engine) -> Iterator[Pipeline]:
    pipeline = Pipeline(migrated_engine)
    pipeline.gitlab([API, SITE])
    pipeline.collect()
    yield pipeline


def assert_nothing_reported_compliant(
    pipeline: Pipeline, *gitlab_ids: int, whole_run: bool = False
) -> None:
    """No pass in the latest results or the API for these projects (and the run, if whole)."""
    run = pipeline.last_evaluation_run()
    assert run.status == "succeeded"
    if whole_run:
        assert "pass" not in run.counts["outcomes"]
    for gitlab_id in gitlab_ids:
        assert all(outcome != "pass" for outcome, _ in pipeline.latest(gitlab_id).values())
        body = get(pipeline.engine, f"/api/v1/gitlab/projects/{gitlab_id}/evaluations")
        assert body["items"], body
        assert all(item["outcome"] != "pass" for item in body["items"])
        assert all(item["observed"] is None for item in body["items"])
    resolved = get(pipeline.engine, "/api/v1/findings?state=resolved&outcome=pass")
    assert resolved["items"] == []


def test_a_healthy_collection_decides_every_rule(pipeline: Pipeline) -> None:
    assert pipeline.latest(1) == {VISIBILITY: ("pass", []), BRANCH: ("pass", [])}
    assert pipeline.latest(2) == {VISIBILITY: ("fail", []), BRANCH: ("fail", [])}
    assert pipeline.findings() == {
        (2, VISIBILITY): ("open", "fail"),
        (2, BRANCH): ("open", "fail"),
    }
    assert pipeline.last_evaluation_run().counts["outcomes"] == {"pass": 2, "fail": 2}
    assert get(pipeline.engine, "/api/v1/gitlab/freshness")["status"] == "fresh"
    assert get(pipeline.engine, "/api/v1/evaluations/freshness")["status"] == "fresh"


@pytest.mark.parametrize("status", [401, 500])
def test_a_failed_collection_after_a_success_decides_nothing(
    pipeline: Pipeline, status: int
) -> None:
    # GitLab now rejects the token (401) or errors (500). The site may well be fixed by now,
    # but nothing confirms it.
    pipeline.fake.set("/projects", httpx.Response(status, json={"message": "no"}))
    pipeline.run(DISCOVER_PROJECTS)
    assert pipeline.last_collection("projects").status == "failed"
    # A failed discovery queues no evaluation; one runs on schedule or on request.
    pipeline.evaluate()

    for gitlab_id in (1, 2):
        assert {r[0] for r in pipeline.latest(gitlab_id).values()} == {"unknown"}
        assert all(r[1][0] == "collection_failed" for r in pipeline.latest(gitlab_id).values())
    assert pipeline.findings() == {
        (2, VISIBILITY): ("open", "unknown"),
        (2, BRANCH): ("open", "unknown"),
    }
    assert pipeline.last_evaluation_run().counts["outcomes"] == {"unknown": 4}
    assert get(pipeline.engine, "/api/v1/gitlab/freshness")["status"] == "failed"
    assert_nothing_reported_compliant(pipeline, 1, 2, whole_run=True)


def test_a_token_revoked_during_protection_reads_leaves_protection_unknown(
    pipeline: Pipeline,
) -> None:
    # The listing completes, then the token is revoked before the protection reads.
    pipeline.gitlab(
        [API, SITE | {"visibility": "private"}],
        protection={1: httpx.Response(401, json={"message": "401 Unauthorized"})},
    )
    start = len(pipeline.fake.requests)
    pipeline.run(DISCOVER_PROJECTS)

    run = pipeline.last_collection("projects")
    assert run.status == "partial"
    assert run.counts["protection"] == {"protected": 0, "unprotected": 0, "unread": 2}
    # The second project was not called after the 401; it is recorded as not read.
    assert "/projects/2/protected_branches" not in pipeline.fake.paths()[start:]
    for gitlab_id in (1, 2):
        assert pipeline.latest(gitlab_id)[BRANCH] == ("unknown", ["value_unknown"])
    # Visibility came from the completed listing, so it is still decided.
    assert pipeline.latest(1)[VISIBILITY] == ("pass", [])
    assert pipeline.latest(2)[VISIBILITY] == ("pass", [])
    assert pipeline.findings() == {
        (2, VISIBILITY): ("resolved", "pass"),
        # Not resolved: the protection was not read.
        (2, BRANCH): ("open", "unknown"),
    }


def test_forbidden_protection_reads_are_unknown_with_their_reason(pipeline: Pipeline) -> None:
    pipeline.gitlab(
        [API, SITE], protection={2: httpx.Response(403, json={"message": "403 Forbidden"})}
    )
    pipeline.collect()

    assert pipeline.latest(1) == {VISIBILITY: ("pass", []), BRANCH: ("pass", [])}
    assert pipeline.latest(2)[BRANCH] == ("unknown", ["value_unknown"])
    body = get(pipeline.engine, "/api/v1/gitlab/projects/2/evaluations")
    branch = next(item for item in body["items"] if item["rule_id"] == BRANCH)
    assert "could not be read (forbidden)" in branch["reason"]
    # The failed read is the evidence linked to the unknown result.
    assert branch["observation_id"] is not None
    assert pipeline.findings()[(2, BRANCH)] == ("open", "unknown")


def test_an_undetected_capability_makes_its_rule_unknown(pipeline: Pipeline) -> None:
    pipeline.fake.set("/projects/1/protected_branches", httpx.Response(403))
    pipeline.run(DETECT_CAPABILITIES)
    pipeline.gitlab([API, SITE])
    pipeline.run(DISCOVER_PROJECTS)

    for gitlab_id in (1, 2):
        assert pipeline.latest(gitlab_id)[BRANCH] == ("unknown", ["capability_unknown"])
    # Visibility needs no capability.
    assert pipeline.latest(1)[VISIBILITY] == ("pass", [])
    assert pipeline.findings()[(2, BRANCH)] == ("open", "unknown")


def test_a_failed_capability_detection_leaves_every_capability_unknown(
    pipeline: Pipeline,
) -> None:
    pipeline.fake.set("/version", httpx.Response(500))
    pipeline.run(DETECT_CAPABILITIES)
    assert pipeline.last_collection("capabilities").status == "failed"
    pipeline.run(DISCOVER_PROJECTS)

    for gitlab_id in (1, 2):
        assert pipeline.latest(gitlab_id)[BRANCH] == ("unknown", ["capability_unknown"])
    body = get(pipeline.engine, "/api/v1/gitlab/projects/1/evaluations")
    branch = next(item for item in body["items"] if item["rule_id"] == BRANCH)
    assert branch["capability"] == {"name": "protected_branches.read", "status": "unknown"}


def test_an_unsupported_capability_is_unsupported_not_compliant(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No probe can report "unsupported" today: detection cannot prove a feature is missing.
    # Stand in for a snapshot that could, so the stored outcome and findings are checked.
    monkeypatch.setattr(
        runner, "capability_statuses", lambda *_: {"protected_branches.read": "unsupported"}
    )
    pipeline.evaluate()

    for gitlab_id in (1, 2):
        assert pipeline.latest(gitlab_id)[BRANCH] == ("unsupported", [])
    # Neither opened nor resolved.
    assert pipeline.findings()[(2, BRANCH)] == ("open", "unsupported")
    assert pipeline.last_evaluation_run().counts["outcomes"] == {
        "pass": 1,
        "fail": 1,
        "unsupported": 2,
    }
    body = get(pipeline.engine, "/api/v1/gitlab/projects/1/evaluations")
    branch = next(item for item in body["items"] if item["rule_id"] == BRANCH)
    assert (branch["outcome"], branch["observed"]) == ("unsupported", None)


def test_a_stale_collection_is_unknown_even_when_the_evidence_said_pass(
    pipeline: Pipeline,
) -> None:
    # Fix the site's visibility: its visibility finding resolves, its branch finding stays.
    pipeline.gitlab([API, SITE | {"visibility": "private"}])
    pipeline.collect()
    assert pipeline.findings() == {
        (2, VISIBILITY): ("resolved", "pass"),
        (2, BRANCH): ("open", "fail"),
    }

    # Thirteen hours pass with no successful collection.
    pipeline.age(CollectionRun)
    pipeline.evaluate()

    for gitlab_id in (1, 2):
        assert all(
            r == ("unknown", ["collection_stale"]) for r in pipeline.latest(gitlab_id).values()
        )
    # The resolved finding is not reopened, and the open one is not resolved.
    assert pipeline.findings() == {
        (2, VISIBILITY): ("resolved", "unknown"),
        (2, BRANCH): ("open", "unknown"),
    }
    # The evidence still says private and protected: it is not current, so it is not used.
    with Session(pipeline.engine) as db:
        payloads = db.scalars(
            select(Observation.payload)
            .join(GitLabProject, GitLabProject.id == Observation.project_id)
            .where(GitLabProject.gitlab_id == 1, Observation.source == PROTECTION_SOURCE)
        ).all()
    assert payloads and payloads[-1]["protected"] is True
    assert get(pipeline.engine, "/api/v1/gitlab/freshness")["status"] == "stale"
    assert_nothing_reported_compliant(pipeline, 1, 2, whole_run=True)


def test_old_evaluation_results_are_reported_stale(pipeline: Pipeline) -> None:
    pipeline.age(EvaluationRun)

    body = get(pipeline.engine, "/api/v1/evaluations/freshness")

    assert body["status"] == "stale"


def test_an_unconfirmed_project_in_a_partial_discovery_is_unknown(pipeline: Pipeline) -> None:
    pipeline.gitlab([API])
    pipeline.fake.set("/projects/2", httpx.Response(500))
    pipeline.run(DISCOVER_PROJECTS)

    assert pipeline.last_collection("projects").status == "partial"
    assert pipeline.latest(1) == {VISIBILITY: ("pass", []), BRANCH: ("pass", [])}
    assert {r[1][0] for r in pipeline.latest(2).values()} == {"target_collection_error"}
    assert pipeline.findings() == {
        (2, VISIBILITY): ("open", "unknown"),
        (2, BRANCH): ("open", "unknown"),
    }


@pytest.mark.parametrize(("status", "state"), [(404, "missing"), (403, "inaccessible")])
def test_a_missing_or_inaccessible_project_keeps_its_findings_open(
    pipeline: Pipeline, status: int, state: str
) -> None:
    pipeline.gitlab([API])
    pipeline.fake.set("/projects/2", httpx.Response(status))
    pipeline.run(DISCOVER_PROJECTS)

    with Session(pipeline.engine) as db:
        site = db.scalars(select(GitLabProject).where(GitLabProject.gitlab_id == 2)).one()
    assert site.state == state
    # Still evaluated while it has open findings, and never as compliant.
    assert {r[1][0] for r in pipeline.latest(2).values()} == {f"target_{state}"}
    assert pipeline.findings() == {
        (2, VISIBILITY): ("open", "unknown"),
        (2, BRANCH): ("open", "unknown"),
    }
    assert_nothing_reported_compliant(pipeline, 2)


def test_a_gap_in_the_group_chain_is_unknown(pipeline: Pipeline) -> None:
    team = group(30, "team")
    app = project(3, "team/app", 30, visibility="public")
    pipeline.gitlab([API, SITE, app], groups=[group(10, "platform"), team])
    pipeline.collect()
    assert pipeline.findings()[(3, VISIBILITY)] == ("open", "fail")

    # The group is moved under a group the token cannot see: a nearer policy may now apply.
    pipeline.gitlab([API, SITE, app], groups=[group(10, "platform"), team | {"parent_id": 99}])
    pipeline.collect()

    assert all(r == ("unknown", ["ancestry_incomplete"]) for r in pipeline.latest(3).values())
    assert pipeline.findings()[(3, VISIBILITY)] == ("open", "unknown")
    # Projects with a complete chain are unaffected.
    assert pipeline.latest(1) == {VISIBILITY: ("pass", []), BRANCH: ("pass", [])}
    assert_nothing_reported_compliant(pipeline, 3)


def test_tampered_evidence_never_backs_a_pass(pipeline: Pipeline) -> None:
    # Someone with database rights makes the site look compliant: the inventory says private
    # and both observations are rewritten, without recomputing their digests.
    with Session(pipeline.engine) as db:
        db.execute(
            update(GitLabProject).where(GitLabProject.gitlab_id == 2).values(visibility="private")
        )
        db.commit()
    pipeline.tamper(2, "GET /projects", "{visibility}", '"private"')
    pipeline.tamper(2, PROTECTION_SOURCE, "{protected}", "true")
    pipeline.evaluate()

    assert pipeline.latest(2) == {
        VISIBILITY: ("unknown", ["evidence_digest_mismatch"]),
        BRANCH: ("unknown", ["evidence_digest_mismatch"]),
    }
    assert pipeline.findings() == {
        (2, VISIBILITY): ("open", "unknown"),
        (2, BRANCH): ("open", "unknown"),
    }
    body = get(pipeline.engine, "/api/v1/gitlab/projects/2/evaluations")
    assert all("integrity check" in item["reason"] for item in body["items"])
    assert_nothing_reported_compliant(pipeline, 2)


def test_an_inventory_that_disagrees_with_its_evidence_is_unknown(pipeline: Pipeline) -> None:
    with Session(pipeline.engine) as db:
        db.execute(
            update(GitLabProject).where(GitLabProject.gitlab_id == 2).values(visibility="private")
        )
        db.commit()
    pipeline.evaluate()

    assert pipeline.latest(2)[VISIBILITY] == ("unknown", ["evidence_mismatch"])
    assert pipeline.findings()[(2, VISIBILITY)] == ("open", "unknown")


def test_a_new_policy_version_continues_the_same_finding(pipeline: Pipeline) -> None:
    before = pipeline.finding_ids()
    stricter = BASELINE.replace("allowed: [private, internal]", "allowed: [private]")
    with Session(pipeline.engine) as db:
        policy = save_version(db, stricter).policy
        root = Target(target_type="instance", target_id=pipeline.instance_id, path=URL, state=None)
        assign(db, pipeline.instance_id, policy, root)
        db.commit()
        new_version = policy.id
    pipeline.evaluate()

    assert pipeline.finding_ids() == before
    with Session(pipeline.engine) as db:
        policies = set(db.scalars(select(Finding.policy_id)))
        versions = db.execute(select(func.count(func.distinct(Evaluation.policy_id)))).scalar_one()
    assert policies == {new_version}
    assert versions == 2
    assert pipeline.findings() == {
        (2, VISIBILITY): ("open", "fail"),
        (2, BRANCH): ("open", "fail"),
    }
