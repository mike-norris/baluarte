"""Collecting change evidence from GitLab: merge requests, direct pushes and deployments."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from gitlab_fake import TOKEN, URL, settings
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte.changes.settings import ChangeEvidenceSettings
from baluarte.db.models import (
    ChangeCursor,
    ChangeRecord,
    CollectionJob,
    CollectionRun,
    GitLabGroup,
    GitLabProject,
    Observation,
)
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab import changes
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import COLLECT_CHANGES, make_collect_changes, queue_changes
from baluarte.integrations.gitlab.release_protections import RELEASE_PROTECTION_SOURCE
from baluarte.jobs.worker import Worker
from baluarte.policy.evaluation.runner import EVALUATE

pytestmark = pytest.mark.db

PROJECT = 7
BASE = f"/api/v4/projects/{PROJECT}"


def ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat().replace("+00:00", "Z")


def sha(prefix: str) -> str:
    return (prefix * 40)[:40]


def mr(
    iid: int,
    *,
    author: str = "dev",
    merged: str | None = None,
    head: str | None = None,
    merge_commit: str | None = None,
    squash_commit: str | None = None,
) -> dict[str, Any]:
    return {
        "iid": iid,
        "title": f"Change {iid}",
        "web_url": f"https://gitlab.example.test/acme/web/-/merge_requests/{iid}",
        "author": {"username": author, "name": "Not kept"},
        "merge_user": {"username": "lead"},
        "created_at": ago(days=3),
        "merged_at": merged or ago(days=2),
        "updated_at": merged or ago(days=2),
        "target_branch": "main",
        "state": "merged",
        "sha": head or sha(f"{iid}h"),
        "merge_commit_sha": merge_commit,
        "squash_commit_sha": squash_commit,
    }


@dataclass
class Repository:
    """A GitLab project's merge requests, commits, pipelines and deployments, served read-only."""

    merge_requests: list[dict[str, Any]] = field(default_factory=list)
    # First-parent commits on main, newest first.
    commits: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[int, Any] = field(default_factory=dict)
    # Pipelines by commit SHA (newest first).
    pipelines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tests: dict[int, Any] = field(default_factory=dict)
    # Merge requests that brought each commit (GET /repository/commits/:sha/merge_requests).
    commit_merge_requests: dict[str, Any] = field(default_factory=dict)
    deployments: list[dict[str, Any]] = field(default_factory=list)
    # Release governance: fields only the single deployment read returns (approvals), releases
    # by tag, protected tags and environments, and group environments by group ID.
    deployment_details: dict[int, dict[str, Any]] = field(default_factory=dict)
    releases: dict[str, dict[str, Any]] = field(default_factory=dict)
    protected_tags: list[dict[str, Any]] = field(default_factory=list)
    environments: list[dict[str, Any]] = field(default_factory=list)
    group_environments: dict[int, Any] = field(default_factory=dict)
    project: dict[str, Any] = field(default_factory=dict)
    # Paths answered with a status instead, e.g. {"/merge_requests": 401}.
    errors: dict[str, int] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def commit(self, commit_sha: str, committed: str | None = None) -> None:
        self.commits.insert(
            0,
            {
                "id": commit_sha,
                "title": f"Commit {commit_sha[:8]}",
                "author_name": "Dev",
                "author_email": "dev@example.test",
                "committed_date": committed or ago(days=2),
            },
        )

    def pipeline(self, commit_sha: str, pipeline_id: int, status: str = "success") -> None:
        self.pipelines.setdefault(commit_sha, []).insert(
            0,
            {
                "id": pipeline_id,
                "sha": commit_sha,
                "status": status,
                "source": "push",
                "finished_at": ago(days=2),
            },
        )

    def deploy(
        self,
        deployment_id: int,
        commit_sha: str,
        ref: str = "main",
        *,
        tag: bool = False,
        deployer: str = "dev",
        pipeline_id: int | None = None,
    ) -> None:
        self.deployments.append(
            {
                "id": deployment_id,
                "iid": deployment_id,
                "ref": ref,
                "sha": commit_sha,
                "status": "success",
                "created_at": ago(days=1),
                "updated_at": ago(days=1),
                "user": {"username": deployer, "name": "Not kept"},
                "environment": {"name": "production"},
                "deployable": {
                    "name": "deploy",
                    "tag": tag,
                    "finished_at": ago(days=1),
                    "pipeline": {"id": pipeline_id} if pipeline_id is not None else None,
                },
            }
        )

    def _pipeline(self, pipeline_id: int) -> dict[str, Any] | None:
        for listed in self.pipelines.values():
            for found in listed:
                if found["id"] == pipeline_id:
                    return found
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        self.requests.append(request)
        path = request.url.path.removeprefix(BASE)
        for prefix, status in self.errors.items():
            if path == prefix:
                return httpx.Response(status, json={"message": f"{status}"})
        params = request.url.params
        if found := re.fullmatch(r"/api/v4/groups/(\d+)/protected_environments", path):
            body = self.group_environments.get(int(found[1]), [])
            if isinstance(body, int):
                return httpx.Response(body, json={"message": str(body)})
            return httpx.Response(200, json=body)
        if path == "":
            return httpx.Response(200, json={"id": PROJECT, **self.project})
        if path == "/protected_tags":
            return httpx.Response(200, json=self.protected_tags)
        if path == "/protected_environments":
            return httpx.Response(200, json=self.environments)
        if found := re.fullmatch(r"/deployments/(\d+)", path):
            number = int(found[1])
            listed = next((d for d in self.deployments if d["id"] == number), None)
            if listed is None or number not in self.deployment_details:
                return httpx.Response(404, json={"message": "404 Not Found"})
            return httpx.Response(200, json=listed | self.deployment_details[number])
        if found := re.fullmatch(r"/releases/(.+)", path):
            release = self.releases.get(found[1])
            if release is None:
                return httpx.Response(404, json={"message": "404 Not Found"})
            return httpx.Response(200, json=release)
        if path == "/deployments":
            assert params["status"] == "success"
            return httpx.Response(200, json=self.deployments)
        if path == "/merge_requests":
            return httpx.Response(200, json=self.merge_requests)
        if found := re.fullmatch(r"/merge_requests/(\d+)", path):
            return httpx.Response(
                200, json=next(m for m in self.merge_requests if m["iid"] == int(found[1]))
            )
        if found := re.fullmatch(r"/merge_requests/(\d+)/approvals", path):
            body = self.approvals.get(int(found[1]), {"approved_by": []})
            if isinstance(body, int):
                return httpx.Response(body, json={"message": str(body)})
            return httpx.Response(200, json=body)
        if found := re.fullmatch(r"/merge_requests/(\d+)/pipelines", path):
            head = next(m for m in self.merge_requests if m["iid"] == int(found[1]))["sha"]
            return httpx.Response(200, json=self.pipelines.get(head, []))
        if found := re.fullmatch(r"/pipelines/(\d+)", path):
            return httpx.Response(200, json=self._pipeline(int(found[1])))
        if found := re.fullmatch(r"/pipelines/(\d+)/test_report_summary", path):
            body = self.tests.get(int(found[1]), {"total": {"count": 0}, "test_suites": []})
            return httpx.Response(200, json=body)
        if path == "/pipelines":
            return httpx.Response(200, json=self.pipelines.get(params["sha"], [])[:1])
        if path == "/repository/commits":
            assert params["first_parent"] == "true"
            return httpx.Response(200, json=self.commits)
        if found := re.fullmatch(r"/repository/commits/(\w+)/merge_requests", path):
            body = self.commit_merge_requests.get(found[1], [])
            if isinstance(body, int):
                return httpx.Response(body, json={"message": str(body)})
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": "404 Not Found"})

    def client(self) -> GitLabClient:
        return GitLabClient(
            settings(), transport=httpx.MockTransport(self.handler), sleep=lambda _: None
        )

    def params(self, path: str) -> list[httpx.QueryParams]:
        return [r.url.params for r in self.requests if r.url.path == BASE + path]


def approved(*names: str) -> dict[str, Any]:
    return {
        "approved_by": [
            {"user": {"username": name, "name": "Not kept"}, "approved_at": ago(days=2)}
            for name in names
        ]
    }


@pytest.fixture
def instance_id(migrated_engine: Engine) -> int:
    with Session(migrated_engine) as db:
        instance = instance_for(db, URL)
        db.add(
            GitLabProject(
                instance_id=instance.id,
                gitlab_id=PROJECT,
                namespace_gitlab_id=4,
                namespace_kind="group",
                path_with_namespace="acme/web",
                name="web",
                visibility="private",
                default_branch="main",
            )
        )
        db.commit()
        return instance.id


def collect(
    engine: Engine, instance_id: int, repo: Repository, **options: Any
) -> changes.ChangeCollection:
    return changes.collect(repo.client(), engine, instance_id, ChangeEvidenceSettings(**options))


def records(engine: Engine) -> dict[tuple[str, Any], ChangeRecord]:
    with Session(engine) as db:
        return {
            changes.change_key(r.kind, r.iid, r.deployment_id, r.sha): r
            for r in db.scalars(select(ChangeRecord))
        }


def versions(engine: Engine, record: ChangeRecord) -> list[Observation]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(Observation)
                .where(
                    Observation.project_id == record.project_id,
                    Observation.source == changes.CHANGE_SOURCES[record.kind],
                    Observation.source_ref
                    == changes.source_ref(
                        PROJECT,
                        {
                            "kind": record.kind,
                            "iid": record.iid,
                            "id": record.deployment_id,
                            "sha": record.sha,
                        },
                    ),
                )
                .order_by(Observation.id)
            )
        )


def cursor(engine: Engine) -> ChangeCursor:
    with Session(engine) as db:
        return db.scalars(select(ChangeCursor)).one()


def busy_repository() -> Repository:
    """A merge commit, a squash merge, a fast-forward merge, a direct push and a deployment."""
    repo = Repository()
    merged = mr(1, merge_commit=sha("m1"))
    squashed = mr(2, author="ops", squash_commit=sha("s2"), merge_commit=sha("m2"))
    fast_forward = mr(3, head=sha("f3"))
    repo.merge_requests = [merged, squashed, fast_forward]
    repo.approvals = {1: approved("reviewer"), 2: approved("ops"), 3: approved("reviewer")}
    for change in repo.merge_requests:
        repo.pipeline(change["sha"], 100 + change["iid"])
    repo.tests[101] = {
        "total": {"count": 3, "success": 3, "failed": 0, "skipped": 0, "error": 0},
        "test_suites": [{"name": "unit"}],
    }
    for commit_sha in (sha("m1"), sha("m2"), sha("f3"), sha("d")):
        repo.commit(commit_sha)
    repo.pipeline(sha("d"), 200, status="failed")
    repo.deploy(300, sha("m1"))
    return repo


def test_a_first_collection_records_each_kind_of_change(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()

    tally = collect(migrated_engine, instance_id, repo)

    found = records(migrated_engine)
    assert set(found) == {
        ("merge_request", 1),
        ("merge_request", 2),
        ("merge_request", 3),
        ("direct_push", sha("d")),
        ("deployment", 300),
    }
    assert tally.changes == {
        "merge_request:new": 3,
        "direct_push:new": 1,
        "deployment:new": 1,
    }
    first = found[("merge_request", 1)]
    assert (first.sha, first.outcome, first.complete) == (sha("m1"), "pass", True)
    assert first.shas == [sha("m1"), sha("1h")]
    squashed = found[("merge_request", 2)]
    assert squashed.sha == sha("m2")
    assert sha("s2") in squashed.shas
    assert squashed.outcome == "fail"
    assert squashed.reasons == ["!2 was approved by its author ops"]
    # A fast-forward merge lands the merge request's own commit.
    assert found[("merge_request", 3)].sha == sha("f3")
    push = found[("direct_push", sha("d"))]
    assert push.outcome == "fail"
    assert "pushed directly" in push.reasons[0]
    assert "the pipeline of commit dddddddd ended failed" in push.reasons
    deployed = found[("deployment", 300)]
    assert (deployed.outcome, deployed.reasons) == (
        "pass",
        [f"deployment 300 ({sha('m1')[:8]}) deployed !1, which was reviewed"],
    )

    [observation] = versions(migrated_engine, first)
    payload = observation.payload
    assert payload is not None and verify(observation)
    assert payload["approvals"]["approved_by"] == [
        {
            "username": "reviewer",
            "approved_at": payload["approvals"]["approved_by"][0]["approved_at"],
        }
    ]
    assert payload["approvals"]["committer_approved"] is None
    assert payload["pipeline"]["tests"] == {
        "total": 3,
        "success": 3,
        "failed": 0,
        "skipped": 0,
        "error": 0,
    }
    assert payload["deployments"]["items"][0]["id"] == 300
    assert payload["web_path"] == "/acme/web/-/merge_requests/1"
    # Names and emails are not kept.
    assert "Not kept" not in str(payload)
    assert "dev@example.test" not in str(versions(migrated_engine, push)[0].payload)
    # Only merge-request commits that no listed merge request named are looked up.
    looked_up = [r.url.path for r in repo.requests if r.url.path.endswith("/merge_requests")]
    assert looked_up == [
        f"{BASE}/merge_requests",
        f"{BASE}/repository/commits/{sha('d')}/merge_requests",
    ]


def test_the_first_collection_reads_back_the_lookback(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    repo.merge_requests.append(mr(9, merged=ago(days=40)))

    collect(migrated_engine, instance_id, repo, lookback_days=30)

    assert ("merge_request", 9) not in records(migrated_engine)
    since = datetime.fromisoformat(repo.params("/merge_requests")[0]["updated_after"])
    assert abs(since - (datetime.now(UTC) - timedelta(days=30))) < timedelta(minutes=1)
    state = cursor(migrated_engine)
    assert abs(state.history_since - since) < timedelta(seconds=1)
    assert state.last_merged_at is not None and state.last_commit_at is not None


def test_a_later_collection_starts_from_the_cursor(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    collect(migrated_engine, instance_id, repo)
    state = cursor(migrated_engine)
    repo.requests.clear()

    tally = collect(migrated_engine, instance_id, repo)

    commits = repo.params("/repository/commits")[0]
    assert datetime.fromisoformat(commits["since"]) == state.last_commit_at - changes.COMMIT_OVERLAP
    assert cursor(migrated_engine).history_since == state.history_since
    # Nothing changed: no new versions.
    # A complete direct push is not read again.
    assert tally.changes == {"merge_request:unchanged": 3}
    assert all(len(versions(migrated_engine, r)) == 1 for r in records(migrated_engine).values())


def test_a_late_deployment_adds_a_version_and_never_rewrites_the_first(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    collect(migrated_engine, instance_id, repo)
    before = records(migrated_engine)[("merge_request", 2)]

    repo.deploy(301, sha("m2"))
    collect(migrated_engine, instance_id, repo)

    after = records(migrated_engine)[("merge_request", 2)]
    first, second = versions(migrated_engine, after)
    assert first.id == before.observation_id
    assert first.payload is not None and first.payload["deployments"]["items"] == []
    assert second.payload is not None
    assert [d["id"] for d in second.payload["deployments"]["items"]] == [301]
    assert after.observation_id == second.id
    assert verify(first) and verify(second)
    deployed = records(migrated_engine)[("deployment", 301)]
    assert deployed.outcome == "fail"
    assert "deployed !2: !2 was approved by its author ops" in deployed.reasons[0]


def test_merge_requests_merged_before_the_redeploy_window_are_not_reread(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    repo.merge_requests.append(mr(5, merged=ago(days=20)))
    collect(migrated_engine, instance_id, repo)
    repo.requests.clear()

    collect(migrated_engine, instance_id, repo, redeploy_days=7)

    approvals = [r.url.path for r in repo.requests if r.url.path.endswith("/approvals")]
    assert f"{BASE}/merge_requests/5/approvals" not in approvals
    assert f"{BASE}/merge_requests/1/approvals" in approvals


def test_a_tier_without_approvals_makes_changes_unknown_not_passing(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    repo.approvals = {1: 403, 2: 404, 3: 403}

    collect(migrated_engine, instance_id, repo)

    found = records(migrated_engine)
    first = found[("merge_request", 1)]
    assert first.outcome == "unknown"
    assert first.complete  # forbidden is not read again every collection
    assert found[("merge_request", 2)].outcome == "unknown"


def test_a_part_that_failed_is_read_again_until_it_is_complete(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    repo.merge_requests[0]["merged_at"] = repo.merge_requests[0]["updated_at"] = ago(days=20)
    repo.approvals[1] = 500
    collect(migrated_engine, instance_id, repo)
    assert records(migrated_engine)[("merge_request", 1)].complete is False

    # Outside the listing now (merged before the re-read window), so read on its own.
    repo.approvals[1] = approved("reviewer")
    tally = collect(migrated_engine, instance_id, repo, redeploy_days=7)

    record = records(migrated_engine)[("merge_request", 1)]
    assert (record.complete, record.outcome) == (True, "pass")
    assert tally.changes["merge_request:updated"] == 1
    assert len(versions(migrated_engine, record)) == 2


def test_a_commit_whose_merge_request_cannot_be_read_holds_the_commit_cursor(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    repo.commit_merge_requests[sha("d")] = 500

    collect(migrated_engine, instance_id, repo)

    assert ("direct_push", sha("d")) not in records(migrated_engine)
    assert cursor(migrated_engine).last_commit_at is None
    with Session(migrated_engine) as db:
        read = db.scalars(
            select(Observation).where(Observation.source == changes.CHANGE_READ_SOURCE)
        ).one()
    assert read.collection_status == "partial"
    assert read.payload is not None and read.payload["listed"]["commits"] == "failed"


def test_a_commit_a_merge_request_brought_is_not_a_direct_push(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = busy_repository()
    # A merge request merged before the history started brought this commit.
    repo.commit(sha("o"))
    repo.commit_merge_requests[sha("o")] = [{"iid": 50, "state": "merged", "target_branch": "main"}]
    # One into another branch does not count.
    repo.commit(sha("x"))
    repo.commit_merge_requests[sha("x")] = [
        {"iid": 51, "state": "merged", "target_branch": "release"}
    ]

    collect(migrated_engine, instance_id, repo)

    found = records(migrated_engine)
    assert ("direct_push", sha("o")) not in found
    assert ("direct_push", sha("x")) in found


def test_a_rejected_token_stops_the_collection(migrated_engine: Engine, instance_id: int) -> None:
    repo = busy_repository()
    repo.errors["/merge_requests"] = 401

    tally = collect(migrated_engine, instance_id, repo)

    assert tally.rejected and tally.failed_projects == 1
    assert records(migrated_engine) == {}
    with Session(migrated_engine) as db:
        read = db.scalars(
            select(Observation).where(Observation.source == changes.CHANGE_READ_SOURCE)
        ).one()
        assert read.collection_status == "failed"
        assert db.scalar(select(func.count()).select_from(ChangeCursor)) == 0


def test_change_records_keep_their_identity(migrated_engine: Engine, instance_id: int) -> None:
    collect(migrated_engine, instance_id, busy_repository())

    with Session(migrated_engine) as db, pytest.raises(Exception, match="change_record"):
        db.execute(
            ChangeRecord.__table__.update().values(sha="0" * 40)  # type: ignore[attr-defined]
        )
        db.flush()


# The job


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run_job(engine: Engine, repo: Repository) -> tuple[CollectionJob, CollectionRun]:
    with Session(engine) as db:
        job_id = queue_changes(db, 1)
        db.commit()
    handler = make_collect_changes(httpx.MockTransport(repo.handler))
    assert Worker(engine, {COLLECT_CHANGES: handler}).run_once()
    with Session(engine) as db:
        run = db.scalars(select(CollectionRun).where(CollectionRun.kind == "changes")).one()
        return db.get_one(CollectionJob, job_id), run


def test_the_job_records_a_run_and_queues_an_evaluation(
    migrated_engine: Engine, instance_id: int, gitlab_env: None
) -> None:
    job, run = run_job(migrated_engine, busy_repository())

    assert job.state == "succeeded"
    assert (run.status, run.seen, run.error) == ("succeeded", 1, None)
    assert run.counts["changes"]["merge_request:new"] == 3
    with Session(migrated_engine) as db:
        kinds = set(db.scalars(select(CollectionJob.kind)))
        run_ids = set(db.scalars(select(Observation.collection_run_id)))
    assert EVALUATE in kinds
    assert run_ids == {run.id}


def test_the_job_fails_when_the_token_is_rejected(
    migrated_engine: Engine, instance_id: int, gitlab_env: None
) -> None:
    repo = busy_repository()
    repo.errors["/deployments"] = 401

    job, run = run_job(migrated_engine, repo)

    assert run.status == "failed"
    assert job.state in ("queued", "failed")


def test_change_collection_is_queued_once_per_discovery(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        first = queue_changes(db, 41)
        assert queue_changes(db, 41) == first
        assert queue_changes(db, 42) != first


# Release governance (docs/RELEASE_GOVERNANCE.md) -------------------------------------------------


def tag_release_repository() -> Repository:
    """A merge request, then a release tag created by ``dev``, approved by ``lead``, deployed."""
    repo = Repository()
    repo.merge_requests = [mr(1, merge_commit=sha("m1"))]
    repo.approvals = {1: approved("reviewer")}
    repo.pipeline(sha("1h"), 101)
    repo.commit(sha("m1"))
    # The tag pipeline, started by whoever pushed the tag.
    repo.pipelines.setdefault(sha("m1"), []).insert(
        0,
        {
            "id": 500,
            "sha": sha("m1"),
            "status": "success",
            "source": "push",
            "ref": "v1-release",
            "tag": True,
            "user": {"username": "dev", "name": "Not kept"},
            "created_at": ago(days=1, hours=2),
            "finished_at": ago(days=1),
        },
    )
    repo.deploy(300, sha("m1"), ref="v1-release", tag=True, deployer="dev", pipeline_id=500)
    repo.deployment_details[300] = {
        "approvals": [
            {
                "user": {"username": "lead", "name": "Not kept"},
                "status": "approved",
                "created_at": ago(days=1, hours=1),
                "comment": "Release window approved",
            }
        ],
        "pending_approval_count": 0,
        "approval_summary": {
            "rules": [
                {
                    "access_level": 40,
                    "required_approvals": 1,
                    "deployment_approvals": [{"status": "approved"}],
                }
            ]
        },
    }
    repo.releases["v1-release"] = {
        "tag_name": "v1-release",
        "name": "Release 1",
        "description": "Fixes and features",
        "author": {"username": "dev", "name": "Not kept"},
        "created_at": ago(days=1, hours=2),
        "released_at": ago(days=1, hours=2),
        "evidences": [
            {"sha": "e" * 40, "filepath": "https://x/evidence.json", "collected_at": ago(days=1)}
        ],
        "assets": {"links": [{"name": "SBOM", "link_type": "other", "url": "https://x/sbom.json"}]},
    }
    return repo


def deployment_payload_of(engine: Engine) -> dict[str, Any]:
    record = records(engine)[("deployment", 300)]
    found = versions(engine, record)[-1].payload
    assert found is not None
    return found


def test_a_tag_deployment_records_who_created_approved_and_deployed_it(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = tag_release_repository()

    collect(migrated_engine, instance_id, repo)

    payload = deployment_payload_of(migrated_engine)
    assert (payload["tag"], payload["ref"], payload["deployer"], payload["pipeline_id"]) == (
        True,
        "v1-release",
        "dev",
        500,
    )
    approvals = payload["deployment_approvals"]
    assert approvals["status"] == "ok"
    assert [(a["username"], a["status"], a["comment"]) for a in approvals["approvals"]] == [
        ("lead", "approved", "Release window approved")
    ]
    assert approvals["rules"] == [
        {"kind": "role", "access_level": 40, "required_approvals": 1, "approved": 1}
    ]
    trigger = payload["trigger"]
    assert (trigger["status"], trigger["user"], trigger["source"], trigger["tag"]) == (
        "ok",
        "dev",
        "push",
        True,
    )
    release = payload["release"]
    assert (release["status"], release["tag_name"], release["author"], release["notes"]) == (
        "ok",
        "v1-release",
        "dev",
        "Fixes and features",
    )
    assert release["evidences"][0]["sha"] == "e" * 40
    assert release["links"] == [
        {"name": "SBOM", "link_type": "other", "url": "https://x/sbom.json"}
    ]
    # Usernames only, never display names.
    assert "Not kept" not in str(payload)
    # The tag deployed the reviewed merge request: judged by it, not failed for its ref.
    assert records(migrated_engine)[("deployment", 300)].outcome == "pass"


def test_a_tag_without_a_release_and_a_tier_without_approvals_are_answers(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = tag_release_repository()
    repo.releases.clear()
    repo.deployment_details[300] = {}  # GitLab Free: no approvals in the deployment

    collect(migrated_engine, instance_id, repo)

    payload = deployment_payload_of(migrated_engine)
    assert payload["release"] == {"status": "none"}
    assert payload["deployment_approvals"]["status"] == "not_supported"
    assert records(migrated_engine)[("deployment", 300)].complete


def test_a_tag_deployment_of_an_unrecorded_commit_is_unknown_not_failed(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = tag_release_repository()
    repo.deployments[0]["sha"] = sha("z")

    collect(migrated_engine, instance_id, repo)

    record = records(migrated_engine)[("deployment", 300)]
    assert record.outcome == "unknown"
    assert "deployed the tag v1-release" in record.reasons[0]


def test_deployments_recorded_before_release_evidence_are_read_again(
    migrated_engine: Engine, instance_id: int
) -> None:
    repo = tag_release_repository()
    details = dict(repo.deployment_details)
    repo.deployment_details.clear()  # the single read fails: recorded without approvals
    repo.errors["/pipelines/500"] = 500

    collect(migrated_engine, instance_id, repo)
    first = deployment_payload_of(migrated_engine)
    assert first["deployment_approvals"]["status"] == "not_found"
    assert first["trigger"]["status"] == "failed"
    assert not records(migrated_engine)[("deployment", 300)].complete

    repo.deployment_details.update(details)
    del repo.errors["/pipelines/500"]
    collect(migrated_engine, instance_id, repo)

    record = records(migrated_engine)[("deployment", 300)]
    assert len(versions(migrated_engine, record)) == 2
    again = deployment_payload_of(migrated_engine)
    assert again["deployment_approvals"]["status"] == "ok"
    assert again["trigger"]["user"] == "dev"
    assert record.complete

    # Complete now: not read again.
    before = len(repo.params("/deployments/300"))
    collect(migrated_engine, instance_id, repo)
    assert len(repo.params("/deployments/300")) == before


def protections(engine: Engine) -> Observation:
    with Session(engine) as db:
        return db.scalars(
            select(Observation)
            .where(Observation.source == RELEASE_PROTECTION_SOURCE)
            .order_by(Observation.id.desc())
        ).first() or pytest.fail("no release protection observation")


def test_the_job_reads_release_protections_with_the_group_environments(
    migrated_engine: Engine, instance_id: int, gitlab_env: None
) -> None:
    with Session(migrated_engine) as db:
        db.add(GitLabGroup(instance_id=instance_id, gitlab_id=4, full_path="acme", name="acme"))
        db.commit()
    repo = tag_release_repository()
    repo.protected_tags = [
        {"name": "*-release", "create_access_levels": [{"id": 1, "access_level": 40}]},
        {"name": "latest", "create_access_levels": [{"id": 2, "user_id": 9, "access_level": None}]},
    ]
    repo.environments = [
        {"name": "production", "required_approval_count": 0, "deploy_access_levels": []}
    ]
    repo.group_environments[4] = [
        {
            "name": "production",
            "required_approval_count": 0,
            "approval_rules": [{"id": 3, "access_level": 40, "required_approvals": 1}],
            "deploy_access_levels": [{"id": 5, "group_id": 8, "access_level": None}],
        }
    ]
    repo.project = {"allow_pipeline_trigger_approve_deployment": True}

    job, run = run_job(migrated_engine, repo)

    assert job.state == "succeeded" and run.status == "succeeded"
    assert run.counts["release_protections"] == {"ok": 1}
    found = protections(migrated_engine)
    assert found.collection_status == "ok" and verify(found)
    payload = found.payload
    assert payload is not None
    assert payload["protected_tags"]["items"] == [
        {"name": "*-release", "create_access_levels": [{"kind": "role", "access_level": 40}]},
        {"name": "latest", "create_access_levels": [{"kind": "user", "access_level": None}]},
    ]
    assert payload["environments"]["items"][0]["approval_rules"] == []
    assert payload["group_environments"] == {
        "status": "ok",
        "groups": [
            {
                "path": "acme",
                "environments": [
                    {
                        "name": "production",
                        "required_approval_count": 0,
                        "approval_rules": [
                            {"kind": "role", "access_level": 40, "required_approvals": 1}
                        ],
                        "deploy_access_levels": [{"kind": "group", "access_level": None}],
                    }
                ],
            }
        ],
    }
    assert payload["pipeline_trigger_approval"] == {"status": "ok", "allowed": True}
    # IDs of users and groups are not kept.
    assert "user_id" not in str(payload) and "group_id" not in str(payload)


def test_release_protections_gitlab_refuses_are_partial_not_failed(
    migrated_engine: Engine, instance_id: int, gitlab_env: None
) -> None:
    repo = tag_release_repository()
    repo.errors["/protected_environments"] = 403
    repo.group_environments[4] = 403

    _job, run = run_job(migrated_engine, repo)

    assert run.status == "succeeded"
    found = protections(migrated_engine)
    assert found.collection_status == "partial"
    assert found.payload is not None
    assert found.payload["environments"]["status"] == "forbidden"
    assert found.payload["group_environments"]["status"] == "forbidden"
    assert found.payload["protected_tags"]["status"] == "ok"
