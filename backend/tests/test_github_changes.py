"""Change evidence from GitHub (roadmap M13.4a).

GitHub is scripted (``test_github.FakeGitHub``); nothing here reaches github.com.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_connections import environment as environment  # the fixture
from test_github import add, collect, github, repo

from baluarte.connections import payload_for
from baluarte.db.models import (
    ChangeRecord,
    CollectionRun,
    Evaluation,
    GitLabInstance,
    Observation,
)
from baluarte.integrations.github.changes import first_parents, reviews_part, runs_part
from baluarte.integrations.github.jobs import COLLECT, make_collect
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.policy.assignments import Target, assign
from baluarte.policy.evaluation.runner import EVALUATE, evaluate
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

BASELINE = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: github-changes
spec:
  scope:
    groups: ["*"]
  rules:
    - id: reviewed
      type: gitlab.change.reviewed
    - id: pipelines
      type: gitlab.change.pipeline_passed
    - id: tested
      type: gitlab.change.pipeline_passed
      require_tests: true
    - id: deployed
      type: gitlab.change.deployed_after_review
"""

H5, M5, H6, M6, P1, B1 = (c * 40 for c in "abcdef")


def ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull(number: int, head: str, merge: str | None, merged_days: float | None) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Change {number}",
        "html_url": f"https://github.com/acme/api/pull/{number}",
        "user": {"login": "grace"},
        "created_at": ago(10),
        "updated_at": ago(merged_days if merged_days is not None else 9),
        "merged_at": ago(merged_days) if merged_days is not None else None,
        "base": {"ref": "main"},
        "head": {"ref": f"topic-{number}", "sha": head},
        "merge_commit_sha": merge,
    }


def commit(sha: str, days: float, *parents: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": f"commit {sha[:4]}\n\nbody",
            "author": {"name": "Grace", "email": "kept-out@example.test", "date": ago(days)},
            "committer": {"name": "Grace", "date": ago(days)},
        },
        "parents": [{"sha": parent} for parent in parents],
    }


def run(run_id: int, sha: str, conclusion: str | None, workflow: int = 1) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": f"workflow {workflow}",
        "workflow_id": workflow,
        "head_sha": sha,
        "event": "pull_request",
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "run_attempt": 1,
        "updated_at": ago(3),
    }


def runs(request: httpx.Request) -> httpx.Response:
    by_sha = {
        H5: [run(1, H5, "success"), run(2, H5, "skipped", workflow=2)],
        # A first attempt failed and a re-run of the same workflow passed; a second failed.
        H6: [run(3, H6, "failure"), run(4, H6, "success"), run(5, H6, "failure", workflow=2)],
    }
    # The re-run of workflow 1 is a later attempt of the same run.
    found = by_sha.get(request.url.params.get("head_sha", ""), [])
    if found and found[0]["head_sha"] == H6:
        found[1]["run_attempt"] = 2
    return httpx.Response(200, json={"total_count": len(found), "workflow_runs": found})


def organization(**overrides: Any) -> Any:
    reviews = {
        5: [{"user": {"login": "ada"}, "state": "APPROVED", "submitted_at": ago(4)}],
        # ada approved, then asked for changes: no approval stands.
        6: [
            {"user": {"login": "ada"}, "state": "APPROVED", "submitted_at": ago(3)},
            {"user": {"login": "ada"}, "state": "CHANGES_REQUESTED", "submitted_at": ago(2.5)},
            {"user": {"login": "lin"}, "state": "COMMENTED", "submitted_at": ago(2.4)},
        ],
    }
    return github(
        **{
            "GET /orgs/acme/repos": [repo(1, "api")],
            "GET /repos/acme/api/deployments": [
                {
                    "id": 70,
                    "sha": M5,
                    "ref": "main",
                    "environment": "production",
                    "created_at": ago(2),
                },
                # Still in progress: not recorded.
                {
                    "id": 71,
                    "sha": M6,
                    "ref": "main",
                    "environment": "production",
                    "created_at": ago(1),
                },
            ],
            "GET /repos/acme/api/deployments/70/statuses": [
                {"state": "success", "created_at": ago(1.9)}
            ],
            "GET /repos/acme/api/deployments/71/statuses": [
                {"state": "in_progress", "created_at": ago(0.9)}
            ],
            "GET /repos/acme/api/pulls": [
                pull(6, H6, M6, 2),
                pull(5, H5, M5, 3),
                # Closed without merging.
                pull(7, B1, None, None),
            ],
            "GET /repos/acme/api/pulls/5/reviews": reviews[5],
            "GET /repos/acme/api/pulls/6/reviews": reviews[6],
            "GET /repos/acme/api/actions/runs": runs,
            # M6 merged pull request 6 (H6 is its branch); M5 merged 5; P1 was pushed directly.
            "GET /repos/acme/api/commits": [
                commit(M6, 2, M5, H6),
                commit(H6, 2.6, B1),
                commit(M5, 3, P1),
                commit(P1, 4, "0" * 40),
            ],
            "GET /repos/acme/api/commits/" + P1 + "/pulls": [],
            **overrides,
        }
    )


def collected_and_evaluated(engine: Engine, fake: Any) -> int:
    connection_id = int(add(engine).json()["id"])
    with Session(engine) as db:
        instance = db.get_one(GitLabInstance, connection_id)
        policy = save_version(db, BASELINE).policy
        assign(
            db,
            instance.id,
            policy,
            Target(
                target_type="instance", target_id=instance.id, path=instance.base_url, state=None
            ),
        )
        db.commit()
    collect(engine, fake, connection_id)
    assert Worker(engine, {EVALUATE: evaluate}).run_once()
    return connection_id


def results(engine: Engine, connection_id: int) -> dict[str, Evaluation]:
    with Session(engine) as db:
        rows = db.scalars(select(Evaluation).where(Evaluation.instance_id == connection_id)).all()
        for row in rows:
            db.expunge(row)
        return {row.rule_id: row for row in rows}


def test_pull_requests_runs_pushes_and_deployments_are_judged(
    migrated_engine: Engine, environment: None
) -> None:
    fake = organization()
    connection_id = collected_and_evaluated(migrated_engine, fake)
    assert {r.method for r in fake.requests} == {"GET"}
    with Session(migrated_engine) as db:
        records = {
            (row.kind, row.iid or row.deployment_id or row.sha[:4]): (row.outcome, row.reasons)
            for row in db.scalars(select(ChangeRecord))
        }
        run = db.scalars(
            select(CollectionRun).where(
                CollectionRun.instance_id == connection_id, CollectionRun.kind == "changes"
            )
        ).one()
    assert run.status == "succeeded", run.error
    assert set(records) == {
        ("merge_request", 5),
        ("merge_request", 6),
        ("direct_push", P1[:4]),
        ("deployment", 70),
    }
    assert records[("merge_request", 5)][0] == "pass"
    outcome, reasons = records[("merge_request", 6)]
    assert outcome == "fail"
    assert "#6 had 0 approvals by someone other than its author; 1 required" in reasons
    assert "the pipeline of #6 ended failed" in reasons
    assert records[("direct_push", P1[:4])][0] == "fail"
    assert records[("deployment", 70)] == (
        "pass",
        ["deployment 70 (bbbbbbbb) deployed #5, which was reviewed"],
    )

    found = results(migrated_engine, connection_id)
    assert {rule: row.outcome for rule, row in found.items()} == {
        "reviewed": "fail",
        "pipelines": "fail",
        "tested": "unsupported",
        "deployed": "pass",
    }
    for row in found.values():
        if row.outcome in ("pass", "fail"):
            assert row.observation_id is not None, (row.rule_id, row.reason)
    assert "no test report API" in found["tested"].reason
    assert found["reviewed"].observed["changes"] == 3
    # The commit author's email is never kept.
    with Session(migrated_engine) as db:
        assert "kept-out" not in str(db.scalars(select(Observation.payload)).all())


def test_unlisted_pull_requests_leave_the_rules_unknown(
    migrated_engine: Engine, environment: None
) -> None:
    fake = organization(
        **{"GET /repos/acme/api/pulls": lambda _: httpx.Response(500, json={"message": "boom"})}
    )
    connection_id = collected_and_evaluated(migrated_engine, fake)
    found = results(migrated_engine, connection_id)
    assert found["reviewed"].outcome == "unknown"
    # The only repository could not be listed, so the changes run failed (as on GitLab).
    assert "changes collection failed" in found["reviewed"].reason


def test_reviews_count_each_reviewers_latest_verdict() -> None:
    part = reviews_part(
        [
            {
                "user": {"login": "ada"},
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-01-01T00:00:00Z",
            },
            {
                "user": {"login": "ada"},
                "state": "COMMENTED",
                "submitted_at": "2026-01-03T00:00:00Z",
            },
            {"user": {"login": "ada"}, "state": "APPROVED", "submitted_at": "2026-01-02T00:00:00Z"},
            {"user": {"login": "lin"}, "state": "APPROVED", "submitted_at": "2026-01-01T00:00:00Z"},
            {
                "user": {"login": "lin"},
                "state": "DISMISSED",
                "submitted_at": "2026-01-02T00:00:00Z",
            },
        ],
        "grace",
    )
    assert [entry["username"] for entry in part["approved_by"]] == ["ada"]
    assert part["self_approved"] is False
    deleted = reviews_part([{"user": None, "state": "APPROVED"}], "grace")
    assert deleted["self_approved"] is None


def test_workflow_runs_combine_into_one_pipeline() -> None:
    assert runs_part([])["status"] == "none"
    assert runs_part([run(1, H5, "success")])["pipeline_status"] == "success"
    assert runs_part([run(1, H5, "success"), run(2, H5, None, 2)])["pipeline_status"] == "running"
    assert runs_part([run(1, H5, "cancelled")])["pipeline_status"] == "canceled"
    assert runs_part([run(1, H5, "skipped")])["pipeline_status"] == "skipped"
    later = run(2, H5, "success")
    later["run_attempt"] = 2
    assert runs_part([run(1, H5, "failure"), later])["pipeline_status"] == "success"


def test_first_parents_follow_the_branch() -> None:
    chain = first_parents(
        [commit(M6, 2, M5, H6), commit(H6, 2.6, B1), commit(M5, 3, P1), commit(P1, 4, "0" * 40)]
    )
    assert [item["sha"] for item in chain] == [M6, M5, P1]


def test_a_deployment_in_progress_is_recorded_once_it_succeeds(
    migrated_engine: Engine, environment: None
) -> None:
    # 72 started before 70 finished, and is still running at the first collection.
    status_72: list[dict[str, Any]] = [{"state": "in_progress", "created_at": ago(2.9)}]
    fake = organization(
        **{
            "GET /repos/acme/api/deployments": [
                {
                    "id": 70,
                    "sha": M5,
                    "ref": "main",
                    "environment": "production",
                    "created_at": ago(2),
                },
                {
                    "id": 72,
                    "sha": M5,
                    "ref": "main",
                    "environment": "production",
                    "created_at": ago(3),
                },
            ],
            "GET /repos/acme/api/deployments/72/statuses": lambda _: httpx.Response(
                200, json=status_72
            ),
        }
    )
    connection_id = collected_and_evaluated(migrated_engine, fake)

    def recorded() -> set[int]:
        with Session(migrated_engine) as db:
            return {
                row.deployment_id
                for row in db.scalars(select(ChangeRecord).where(ChangeRecord.kind == "deployment"))
                if row.deployment_id is not None
            }

    assert recorded() == {70}
    # It succeeds an hour after 70 finished: older than the cursor's overlap.
    status_72.insert(0, {"state": "success", "created_at": ago(0.5)})
    fake.requests.clear()
    # The next scheduled collection (requests in the same minute share one job).
    with Session(migrated_engine) as db:
        queue.enqueue(db, COLLECT, f"next:{connection_id}", payload_for(connection_id))
        db.commit()
    assert Worker(migrated_engine, {COLLECT: make_collect(fake.transport)}).run_once()
    assert recorded() == {70, 72}
    paths = [request.url.path for request in fake.requests]
    # A recorded deployment's statuses are not read again.
    assert "/repos/acme/api/deployments/70/statuses" not in paths
