"""Release checks: each production release judged against its practice (RG.3)."""

from pathlib import Path
from typing import Any

import pytest
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.orm import Session
from test_change_collection import PROJECT, tag_release_repository

from baluarte.changes.settings import ChangeEvidenceSettings
from baluarte.db.models import (
    CollectionRun,
    Evaluation,
    EvaluationRun,
    GitLabGroup,
    GitLabProject,
    Observation,
)
from baluarte.evidence.observations import Observed, record
from baluarte.integrations.gitlab import changes
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.release_protections import RELEASE_PROTECTION_SOURCE
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.assignments import Target, assign
from baluarte.policy.evaluation.runner import evaluate_all
from baluarte.policy.store import save_version
from baluarte.releases import checks, practices

# --- one release ---

TAG = practices.validated("release_tag", None)
PROTECTED = {"protected_tags": {"status": "ok", "items": [{"name": "*-release"}]}}


def release(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "deployment",
        "id": 300,
        "environment": "production",
        "ref": "v1-release",
        "sha": "a" * 40,
        "tag": True,
        "deployer": "dev",
        "finished_at": "2026-11-20T10:00:00Z",
        "deployment_approvals": {
            "status": "ok",
            "approvals": [
                {"username": "lead", "status": "approved", "created_at": "2026-11-20T09:00:00Z"}
            ],
        },
        "trigger": {"status": "ok", "user": "dev", "source": "push"},
    }
    return payload | overrides


REVIEWED = {
    "a" * 40: {
        "kind": "merge_request",
        "iid": 1,
        "author": "dev",
        "sha": "a" * 40,
        "approvals": {"status": "ok", "approved_by": [{"username": "reviewer"}]},
    }
}


def judged(payload: dict[str, Any], settings: Any = TAG, protections: Any = PROTECTED) -> Any:
    return checks.judge(payload, settings, protections, REVIEWED, "main")


def test_a_release_with_separate_people_passes() -> None:
    result = judged(release())
    assert result.verdict == "pass", result.reasons
    assert (result.started_by, result.approvers, result.deployer) == ("dev", ["lead"], "dev")


def test_the_tag_creator_or_deployer_approving_fails_with_the_people_named() -> None:
    self_approved = release(
        deployment_approvals={
            "status": "ok",
            "approvals": [
                {"username": "dev", "status": "approved", "created_at": "2026-11-20T09:00:00Z"}
            ],
        }
    )
    result = judged(self_approved)
    assert result.verdict == "fail"
    assert (
        "deployment 300 (aaaaaaaa) was approved by dev, who started the release" in result.reasons
    )
    assert "deployment 300 (aaaaaaaa) was deployed by dev, who also approved it" in result.reasons

    # A practice may allow it.
    lenient = TAG.model_copy(update={"creator_may_approve": True, "deployer_may_approve": True})
    assert judged(self_approved, lenient).verdict == "pass"

    # A separate deployer who approved fails on its own.
    deployer = release(deployer="lead")
    result = judged(deployer)
    assert result.verdict == "fail"
    assert result.reasons == [
        "deployment 300 (aaaaaaaa) was deployed by lead, who also approved it"
    ]


def test_approvals_must_be_enough_and_given_before_the_deployment_finished() -> None:
    late = release(
        deployment_approvals={
            "status": "ok",
            "approvals": [
                {"username": "lead", "status": "approved", "created_at": "2026-11-20T11:00:00Z"},
                {"username": "ops", "status": "rejected", "created_at": "2026-11-20T09:00:00Z"},
            ],
        }
    )
    result = judged(late)
    assert result.verdict == "fail"
    assert result.reasons == [
        "deployment 300 (aaaaaaaa) had 0 approvals before it was deployed; 1 required"
    ]
    two = TAG.model_copy(update={"required_approvals": 2})
    assert "1 approval before it was deployed; 2 required" in judged(release(), two).reasons[0]


def test_refs_and_protection() -> None:
    branch = judged(release(ref="main", tag=False))
    assert branch.verdict == "fail"
    assert branch.reasons[0] == (
        "deployment 300 (aaaaaaaa) deployed the default branch main, which the practice does not "
        "expect"
    )
    unprotected = judged(
        release(ref="v1-release"),
        protections={"protected_tags": {"status": "ok", "items": [{"name": "202*"}]}},
    )
    assert unprotected.reasons == [
        "the tag v1-release of deployment 300 (aaaaaaaa) is not protected"
    ]
    unread = judged(release(), protections={"protected_tags": {"status": "forbidden"}})
    assert (unread.verdict, unread.reasons) == (
        "unknown",
        ["the project's protected tags were not read"],
    )
    assert judged(release(), protections=None).verdict == "unknown"


def test_what_cannot_be_read_is_unknown_never_a_pass() -> None:
    free = judged(release(deployment_approvals={"status": "not_supported"}))
    assert free.verdict == "unknown"
    assert "GitLab did not report its approvals (Premium or Ultimate)" in free.reasons[0]
    no_trigger = judged(release(trigger={"status": "failed", "error": "x"}))
    assert no_trigger.verdict == "unknown"
    assert no_trigger.reasons == [
        "deployment 300 (aaaaaaaa): who started it could not be read (failed)"
    ]
    untold = judged(release(tag=None))
    assert untold.verdict == "unknown"


def test_merge_request_review_of_what_shipped_counts_when_the_practice_requires_it() -> None:
    unreviewed = {
        "a" * 40: {
            **REVIEWED["a" * 40],
            "approvals": {"status": "ok", "approved_by": [{"username": "dev"}]},
        }
    }
    failed = checks.judge(release(), TAG, PROTECTED, unreviewed, "main")
    assert failed.verdict == "fail"
    assert failed.reasons == [
        "deployment 300 (aaaaaaaa) deployed !1: !1 was approved by its author dev"
    ]
    relaxed = TAG.model_copy(update={"require_merge_request_review": False})
    assert checks.judge(release(), relaxed, PROTECTED, unreviewed, "main").verdict == "pass"


# --- the rule end to end ---

SETTINGS = GitLabSettings(url=URL, token=TOKEN)
POLICY = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: release-governance
spec:
  scope:
    groups: ["*"]
  rules:
    - id: releases
      type: gitlab.release.follows_practice
      window_days: 30
"""


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


class Releases:
    def __init__(self, engine: Engine, repo: Any = None) -> None:
        self.engine = engine
        self.repo = repo or tag_release_repository()
        with Session(engine) as db:
            instance = instance_for(db, URL)
            self.instance = instance
            self.instance_id = instance.id
            db.add(GitLabGroup(instance_id=instance.id, gitlab_id=4, full_path="acme", name="acme"))
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
            policy = save_version(db, POLICY).policy
            self.root = Target(target_type="instance", target_id=instance.id, path=URL, state=None)
            assign(db, instance.id, policy, self.root)
            db.commit()
        changes.collect(self.repo.client(), engine, self.instance_id, ChangeEvidenceSettings())
        with Session(engine) as db:
            for kind in ("projects", "changes"):
                db.execute(
                    insert(CollectionRun).values(
                        instance_id=self.instance_id,
                        kind=kind,
                        status="succeeded",
                        started_at=text("now()"),
                        finished_at=text("now()"),
                    )
                )
            db.commit()

    def practice(self, **settings: Any) -> None:
        with Session(self.engine) as db:
            instance = instance_for(db, URL)
            found = practices.create(db, "tag-releases", "release_tag", settings, None, "test")
            practices.assign(db, instance, found, self.root, "test")
            db.commit()

    def protections(self, patterns: list[str]) -> None:
        with Session(self.engine) as db:
            project = db.scalars(select(GitLabProject)).one()
            record(
                db,
                self.instance_id,
                "project_id",
                [
                    Observed(
                        target_id=project.id,
                        source=RELEASE_PROTECTION_SOURCE,
                        source_ref=f"/projects/{PROJECT}",
                        collection_status="ok",
                        payload={
                            "protected_tags": {
                                "status": "ok",
                                "items": [
                                    {"name": p, "create_access_levels": []} for p in patterns
                                ],
                            }
                        },
                    )
                ],
            )
            db.commit()

    def result(self) -> Evaluation:
        run_id, _ = evaluate_all(self.engine, SETTINGS)
        with Session(self.engine) as db:
            db.get_one(EvaluationRun, run_id)
            return db.scalars(
                select(Evaluation).where(
                    Evaluation.evaluation_run_id == run_id, Evaluation.rule_id == "releases"
                )
            ).one()


@pytest.mark.db
def test_without_a_practice_the_rule_is_unknown(migrated_engine: Engine) -> None:
    world = Releases(migrated_engine)
    world.protections(["*-release"])

    result = world.result()

    assert result.outcome == "unknown"
    assert "No release practice applies to project acme/web" in (result.reason or "")


@pytest.mark.db
def test_a_tag_release_with_separate_people_passes_and_rests_on_its_evidence(
    migrated_engine: Engine,
) -> None:
    world = Releases(migrated_engine)
    world.practice()
    world.protections(["*-release"])

    result = world.result()

    assert result.outcome == "pass", result.reason
    observed = result.observed
    assert observed is not None
    assert (observed["changes"], observed["passed"]) == (1, 1)
    assert observed["practice"]["name"] == "tag-releases"
    assert observed["practice"]["decided_by"]["target_type"] == "instance"
    with Session(migrated_engine) as db:
        sources = {db.get_one(Observation, i).source for i in result.observation_ids or []}
    # The deployment, the merge request it shipped and the release protections.
    assert sources == {
        changes.DEPLOYMENT_SOURCE,
        changes.MERGE_REQUEST_SOURCE,
        RELEASE_PROTECTION_SOURCE,
    }


@pytest.mark.db
def test_a_release_its_creator_approved_fails_with_the_people_named(
    migrated_engine: Engine,
) -> None:
    repo = tag_release_repository()
    repo.deployment_details[300]["approvals"][0]["user"]["username"] = "dev"
    world = Releases(migrated_engine, repo)
    world.practice()
    world.protections(["*-release"])

    result = world.result()

    assert result.outcome == "fail"
    observed = result.observed
    assert observed is not None
    failing = observed["failing"][0]
    assert (failing["ref"], failing["started_by"], failing["approvers"], failing["deployer"]) == (
        "v1-release",
        "dev",
        ["dev"],
        "dev",
    )
    assert "was approved by dev, who started the release" in (result.reason or "")


@pytest.mark.db
def test_an_unprotected_tag_fails_and_tampered_protections_are_never_a_result(
    migrated_engine: Engine,
) -> None:
    world = Releases(migrated_engine)
    world.practice()
    world.protections(["202*"])
    assert world.result().outcome == "fail"

    with migrated_engine.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text(
                "UPDATE observation SET payload = jsonb_set(payload, "
                "'{protected_tags,items,0,name}', '\"*-release\"') WHERE source = :source"
            ),
            {"source": RELEASE_PROTECTION_SOURCE},
        )

    result = world.result()
    assert result.outcome == "unknown"
    assert result.unknown_reasons == ["evidence_digest_mismatch"]
