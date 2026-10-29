"""Evaluating a project's effective policies against the stored inventory."""

from datetime import timedelta

import pytest
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session
from test_policy_schema import BASELINE

from baluarte.db.models import CollectionRun, GitLabGroup, GitLabInstance, GitLabProject
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.assignments import Target, assign, resolve_project
from baluarte.policy.evaluation.model import RuleResult
from baluarte.policy.evaluation.project import collection_contexts, evaluate_project
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

SETTINGS = GitLabSettings(url=URL, token=TOKEN)
PRIVATE_ONLY = BASELINE.replace("name: default-gitlab-baseline", "name: a-private-only").replace(
    "allowed: [private, internal]", "allowed: [private]"
)

# (gitlab_id, path_with_namespace, namespace, visibility, state, last_error)
PROJECTS = [
    (100, "platform/api", 10, "private", "active", None),
    (101, "platform/site", 10, "public", "active", None),
    (102, "platform/legacy", 10, None, "active", None),
    (103, "platform/gone", 10, "private", "missing", None),
    (104, "platform/flaky", 10, "private", "active", "GitLab returned 502 for /projects/104"),
    (105, "alice/dotfiles", 500, "private", "active", None),
    (106, "bob/notes", 501, "private", "active", None),
]
# Namespace kinds recorded by discovery; 105 predates them, so its kind is unknown.
NAMESPACE_KINDS = {
    100: "group",
    101: "group",
    102: "group",
    103: "group",
    104: "group",
    106: "user",
}


@pytest.fixture
def instance_id(migrated_engine: Engine) -> int:
    with Session(migrated_engine) as db:
        instance = instance_for(db, URL)
        db.execute(
            insert(GitLabGroup).values(
                instance_id=instance.id, gitlab_id=10, full_path="platform", name="platform"
            )
        )
        for gitlab_id, path, namespace, visibility, state, error in PROJECTS:
            db.execute(
                insert(GitLabProject).values(
                    instance_id=instance.id,
                    gitlab_id=gitlab_id,
                    namespace_gitlab_id=namespace,
                    namespace_kind=NAMESPACE_KINDS.get(gitlab_id),
                    path_with_namespace=path,
                    name=path.rsplit("/", 1)[-1],
                    visibility=visibility,
                    state=state,
                    last_error=error,
                )
            )
        root = Target(target_type="instance", target_id=instance.id, path=URL, state=None)
        assign(db, instance.id, save_version(db, BASELINE).policy, root)
        db.commit()
        return instance.id


def add_run(engine: Engine, instance_id: int, kind: str, status: str, age: timedelta) -> None:
    with Session(engine) as db:
        finished = func.now() - age
        db.execute(
            insert(CollectionRun).values(
                instance_id=instance_id,
                kind=kind,
                status=status,
                started_at=finished,
                finished_at=finished,
            )
        )
        db.commit()


def evaluate(
    engine: Engine, instance_id: int, gitlab_id: int, settings: GitLabSettings | None = SETTINGS
) -> list[RuleResult]:
    with Session(engine) as db:
        instance = db.get_one(GitLabInstance, instance_id)
        project = db.scalars(
            select(GitLabProject).where(GitLabProject.gitlab_id == gitlab_id)
        ).one()
        collections = collection_contexts(db, instance_id, settings)
        results = evaluate_project(db, project, resolve_project(db, instance, project), collections)
        assert not db.new and not db.dirty and not db.deleted
        return results


def outcomes(results: list[RuleResult]) -> list[tuple[str, str, str, tuple[str, ...]]]:
    return [(r.policy.name, r.rule_id, r.outcome, r.unknown_reasons) for r in results]


@pytest.fixture
def fresh(migrated_engine: Engine, instance_id: int) -> int:
    add_run(migrated_engine, instance_id, "projects", "succeeded", timedelta(minutes=5))
    return instance_id


@pytest.mark.parametrize(
    ("gitlab_id", "outcome", "reasons"),
    [
        (100, "pass", ()),
        (101, "fail", ()),
        (102, "unknown", ("value_unknown",)),
        (103, "unknown", ("target_missing",)),
        (104, "unknown", ("target_collection_error",)),
        # Namespace kind not recorded yet: its namespace is not a known group, so incomplete.
        (105, "unknown", ("ancestry_incomplete",)),
        # A personal project resolves straight to the instance.
        (106, "pass", ()),
    ],
)
def test_visibility_outcomes_from_the_inventory(
    migrated_engine: Engine, fresh: int, gitlab_id: int, outcome: str, reasons: tuple[str, ...]
) -> None:
    results = evaluate(migrated_engine, fresh, gitlab_id)

    # Only the visibility rule has an evaluator; the branch rule is left for BAL-020.
    assert outcomes(results) == [
        ("default-gitlab-baseline", "project-visibility", outcome, reasons)
    ]


def test_a_result_names_the_pinned_version_and_the_collection(
    migrated_engine: Engine, fresh: int
) -> None:
    (result,) = evaluate(migrated_engine, fresh, 101)

    assert result.policy.name == "default-gitlab-baseline"
    assert result.policy.version == 1
    assert len(result.policy.yaml_digest) == 64
    assert result.target.path == "platform/site"
    assert result.observed == "public"
    assert result.expected == {"allowed": ["private", "internal"]}
    assert result.collection.status == "fresh"
    assert result.collection.last_success_at is not None
    assert result.observed_at is not None
    assert result.evaluated_at.tzinfo is not None


def test_every_effective_policy_is_evaluated_in_name_order(
    migrated_engine: Engine, fresh: int
) -> None:
    with Session(migrated_engine) as db:
        group = Target(target_type="group", target_id=10, path="platform", state="active")
        assign(db, fresh, save_version(db, PRIVATE_ONLY).policy, group)
        db.commit()

    results = evaluate(migrated_engine, fresh, 101)

    assert outcomes(results) == [
        ("a-private-only", "project-visibility", "fail", ()),
        ("default-gitlab-baseline", "project-visibility", "fail", ()),
    ]
    assert evaluate(migrated_engine, fresh, 100)[0].outcome == "pass"


@pytest.mark.parametrize(
    ("status", "age", "reason"),
    [
        ("failed", timedelta(minutes=5), "collection_failed"),
        ("succeeded", timedelta(days=2), "collection_stale"),
    ],
)
def test_collection_freshness_makes_every_project_unknown(
    migrated_engine: Engine, instance_id: int, status: str, age: timedelta, reason: str
) -> None:
    add_run(migrated_engine, instance_id, "projects", "succeeded", timedelta(days=3))
    add_run(migrated_engine, instance_id, "projects", status, age)

    for gitlab_id in (100, 101):
        assert outcomes(evaluate(migrated_engine, instance_id, gitlab_id))[0][2:] == (
            "unknown",
            (reason,),
        )


def test_never_collected(migrated_engine: Engine, instance_id: int) -> None:
    (result,) = evaluate(migrated_engine, instance_id, 100)

    assert result.unknown_reasons == ("collection_never",)


def test_not_configured(migrated_engine: Engine, instance_id: int) -> None:
    (result,) = evaluate(migrated_engine, instance_id, 100, settings=None)

    assert result.unknown_reasons == ("collection_not_configured",)
    assert result.collection.status == "not_configured"


def test_a_partial_collection_decides_confirmed_projects(
    migrated_engine: Engine, instance_id: int
) -> None:
    add_run(migrated_engine, instance_id, "projects", "partial", timedelta(minutes=5))

    assert outcomes(evaluate(migrated_engine, instance_id, 100))[0][2] == "pass"
    assert outcomes(evaluate(migrated_engine, instance_id, 104))[0][2:] == (
        "unknown",
        ("target_collection_error",),
    )


def test_other_collections_do_not_affect_visibility(migrated_engine: Engine, fresh: int) -> None:
    add_run(migrated_engine, fresh, "groups", "failed", timedelta(minutes=1))
    add_run(migrated_engine, fresh, "capabilities", "failed", timedelta(minutes=1))

    assert outcomes(evaluate(migrated_engine, fresh, 100))[0][2] == "pass"


def test_no_effective_policies_means_no_results(migrated_engine: Engine, fresh: int) -> None:
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, fresh)
        project = db.scalars(select(GitLabProject).where(GitLabProject.gitlab_id == 100)).one()
        resolution = resolve_project(db, instance, project).model_copy(update={"policies": []})

        assert (
            evaluate_project(db, project, resolution, collection_contexts(db, fresh, SETTINGS))
            == []
        )


def test_a_resolution_for_another_project_is_rejected(migrated_engine: Engine, fresh: int) -> None:
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, fresh)
        api, site = (
            db.scalars(select(GitLabProject).where(GitLabProject.gitlab_id == gid)).one()
            for gid in (100, 101)
        )

        with pytest.raises(ValueError, match="not for this project"):
            evaluate_project(db, api, resolve_project(db, instance, site), {})
