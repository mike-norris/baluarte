"""Evaluating a project's effective policies against the stored inventory."""

from datetime import timedelta
from typing import Any

import pytest
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.orm import Session
from test_policy_schema import BASELINE

from baluarte.db.models import CollectionRun, GitLabGroup, GitLabInstance, GitLabProject
from baluarte.evidence.observations import Observed, record
from baluarte.integrations.gitlab.branches import PROTECTION_SOURCE
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.assignments import Target, assign, resolve_project
from baluarte.policy.evaluation.model import RuleResult
from baluarte.policy.evaluation.project import (
    capability_statuses,
    collection_contexts,
    evaluate_project,
)
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

SETTINGS = GitLabSettings(url=URL, token=TOKEN)
PRIVATE_ONLY = BASELINE.replace("name: default-gitlab-baseline", "name: a-private-only").replace(
    "allowed: [private, internal]", "allowed: [private]"
)
VISIBILITY = "project-visibility"
BRANCH = "default-branch-protected"

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
# Default-branch protection as read by discovery: True protected, False unprotected.
PROTECTED = {100: True, 101: False, 102: True, 103: True, 104: True, 105: True, 106: True}


def protection(branch: str, protected: bool) -> dict[str, Any]:
    rules = [{"name": branch}] if protected else []
    return {"branch": branch, "protected": protected, "rules": rules}


def observe_protection(
    engine: Engine,
    instance_id: int,
    gitlab_id: int,
    payload: dict[str, Any] | None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    with Session(engine) as db:
        row_id = db.execute(
            select(GitLabProject.id).where(GitLabProject.gitlab_id == gitlab_id)
        ).scalar_one()
        observed = Observed(
            target_id=row_id,
            source=PROTECTION_SOURCE,
            source_ref=f"/projects/{gitlab_id}/protected_branches",
            collection_status=status,
            payload=payload,
            error=error,
        )
        record(db, instance_id, "project_id", [observed])
        db.commit()


def set_probe(engine: Engine, instance_id: int, status: str | None) -> None:
    """Store a capability snapshot whose protected-branch probe has ``status``."""
    probes = {"projects.list": {"status": "supported", "endpoint": "GET /projects"}}
    if status is not None:
        probes["protected_branches.read"] = {
            "status": status,
            "endpoint": "GET /projects/:id/protected_branches",
        }
    snapshot = {"status": "ok", "probes": probes}
    with Session(engine) as db:
        db.execute(
            update(GitLabInstance)
            .where(GitLabInstance.id == instance_id)
            .values(capability_snapshot=snapshot)
        )
        db.commit()


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
                    default_branch="main",
                    state=state,
                    last_error=error,
                )
            )
        root = Target(target_type="instance", target_id=instance.id, path=URL, state=None)
        assign(db, instance.id, save_version(db, BASELINE).policy, root)
        db.commit()
        instance_id = instance.id
    set_probe(migrated_engine, instance_id, "supported")
    for gitlab_id, protected in PROTECTED.items():
        observe_protection(migrated_engine, instance_id, gitlab_id, protection("main", protected))
    return instance_id


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


def rule(results: list[RuleResult], rule_id: str) -> RuleResult:
    (found,) = (result for result in results if result.rule_id == rule_id)
    return found


def set_default_branch(engine: Engine, gitlab_id: int, branch: str | None) -> None:
    with Session(engine) as db:
        db.execute(
            update(GitLabProject)
            .where(GitLabProject.gitlab_id == gitlab_id)
            .values(default_branch=branch)
        )
        db.commit()


@pytest.fixture
def fresh(migrated_engine: Engine, instance_id: int) -> int:
    add_run(migrated_engine, instance_id, "projects", "succeeded", timedelta(minutes=5))
    return instance_id


@pytest.mark.parametrize(
    ("gitlab_id", "visibility", "branch"),
    [
        (100, ("pass", ()), ("pass", ())),
        (101, ("fail", ()), ("fail", ())),
        (102, ("unknown", ("value_unknown",)), ("pass", ())),
        (103, ("unknown", ("target_missing",)), ("unknown", ("target_missing",))),
        (
            104,
            ("unknown", ("target_collection_error",)),
            ("unknown", ("target_collection_error",)),
        ),
        # Namespace kind not recorded yet: its namespace is not a known group, so incomplete.
        (105, ("unknown", ("ancestry_incomplete",)), ("unknown", ("ancestry_incomplete",))),
        # A personal project resolves straight to the instance.
        (106, ("pass", ()), ("pass", ())),
    ],
)
def test_outcomes_from_the_inventory(
    migrated_engine: Engine,
    fresh: int,
    gitlab_id: int,
    visibility: tuple[str, tuple[str, ...]],
    branch: tuple[str, tuple[str, ...]],
) -> None:
    results = evaluate(migrated_engine, fresh, gitlab_id)

    # Rules in the order the policy lists them.
    assert outcomes(results) == [
        ("default-gitlab-baseline", VISIBILITY, *visibility),
        ("default-gitlab-baseline", BRANCH, *branch),
    ]


def test_a_result_names_the_pinned_version_and_the_collection(
    migrated_engine: Engine, fresh: int
) -> None:
    result = rule(evaluate(migrated_engine, fresh, 101), VISIBILITY)

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


def test_a_branch_result_records_what_was_observed(migrated_engine: Engine, fresh: int) -> None:
    passed = rule(evaluate(migrated_engine, fresh, 100), BRANCH)
    failed = rule(evaluate(migrated_engine, fresh, 101), BRANCH)

    assert passed.expected == {"required": True}
    assert passed.observed == protection("main", True)
    assert passed.source == "GET /projects/:id/protected_branches"
    assert passed.capability.model_dump() == {
        "name": "protected_branches.read",
        "status": "supported",
    }
    assert passed.reason == "Default branch main is protected by main."
    assert failed.observed == protection("main", False)
    assert failed.reason == (
        "Default branch main is not protected; the policy requires protection."
    )


@pytest.mark.parametrize(
    ("probe", "outcome", "reasons"),
    [
        (None, "unknown", ("capability_unknown",)),
        ("forbidden", "unknown", ("capability_unknown",)),
        ("not_found", "unknown", ("capability_unknown",)),
        ("skipped", "unknown", ("capability_unknown",)),
    ],
)
def test_an_undetected_capability_makes_the_branch_rule_unknown(
    migrated_engine: Engine, fresh: int, probe: str | None, outcome: str, reasons: tuple[str, ...]
) -> None:
    set_probe(migrated_engine, fresh, probe)

    results = evaluate(migrated_engine, fresh, 100)

    assert (rule(results, BRANCH).outcome, rule(results, BRANCH).unknown_reasons) == (
        outcome,
        reasons,
    )
    assert rule(results, BRANCH).observed is None
    # Visibility needs no capability.
    assert rule(results, VISIBILITY).outcome == "pass"


def test_without_a_capability_snapshot_every_capability_is_unknown(
    migrated_engine: Engine, fresh: int
) -> None:
    with Session(migrated_engine) as db:
        db.execute(update(GitLabInstance).values(capability_snapshot=None))
        db.commit()
        assert capability_statuses(db, fresh) == {}

    assert rule(evaluate(migrated_engine, fresh, 100), BRANCH).unknown_reasons == (
        "capability_unknown",
    )


def test_a_failed_capability_detection_counts_as_undetected(
    migrated_engine: Engine, fresh: int
) -> None:
    with Session(migrated_engine) as db:
        db.execute(
            update(GitLabInstance).values(capability_snapshot={"status": "failed", "probes": {}})
        )
        db.commit()
        assert capability_statuses(db, fresh) == {}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("empty", "has no default branch (an empty repository)"),
        ("never_read", "has not been read yet"),
        ("forbidden", "could not be read (forbidden): the token may not read"),
        ("failed", "could not be read (failed): GitLab returned 500"),
        ("renamed", "was last read for branch main, but the default branch is now trunk"),
    ],
)
def test_branch_protection_that_is_not_known_is_unknown(
    migrated_engine: Engine, fresh: int, change: str, reason: str
) -> None:
    gitlab_id = 100
    if change == "empty":
        set_default_branch(migrated_engine, gitlab_id, None)
    elif change == "never_read":
        gitlab_id = 107
        with Session(migrated_engine) as db:
            db.execute(
                insert(GitLabProject).values(
                    instance_id=fresh,
                    gitlab_id=107,
                    namespace_gitlab_id=10,
                    namespace_kind="group",
                    path_with_namespace="platform/new",
                    name="new",
                    visibility="private",
                    default_branch="main",
                )
            )
            db.commit()
    elif change == "forbidden":
        observe_protection(
            migrated_engine,
            fresh,
            100,
            None,
            "forbidden",
            "the token may not read /projects/100/protected_branches (403)",
        )
    elif change == "failed":
        observe_protection(
            migrated_engine,
            fresh,
            100,
            None,
            "failed",
            "GitLab returned 500 for /projects/100/protected_branches",
        )
    else:
        set_default_branch(migrated_engine, gitlab_id, "trunk")

    result = rule(evaluate(migrated_engine, fresh, gitlab_id), BRANCH)

    assert (result.outcome, result.unknown_reasons) == ("unknown", ("value_unknown",))
    assert reason in result.reason
    assert result.observed is None and result.observed_at is None


def test_a_later_successful_read_replaces_a_failed_one(migrated_engine: Engine, fresh: int) -> None:
    observe_protection(migrated_engine, fresh, 101, None, "failed", "GitLab returned 500")
    assert rule(evaluate(migrated_engine, fresh, 101), BRANCH).outcome == "unknown"

    observe_protection(migrated_engine, fresh, 101, protection("main", True))

    assert rule(evaluate(migrated_engine, fresh, 101), BRANCH).outcome == "pass"


def test_required_false_asserts_nothing(migrated_engine: Engine, fresh: int) -> None:
    optional = BASELINE.replace("name: default-gitlab-baseline", "name: a-optional").replace(
        "required: true", "required: false"
    )
    with Session(migrated_engine) as db:
        group = Target(target_type="group", target_id=10, path="platform", state="active")
        assign(db, fresh, save_version(db, optional).policy, group)
        db.commit()

    results = [r for r in evaluate(migrated_engine, fresh, 101) if r.policy.name == "a-optional"]
    branch = rule(results, BRANCH)

    assert (branch.outcome, branch.expected) == ("pass", {"required": False})
    assert branch.reason == (
        "The policy does not require protection; default branch main is not protected."
    )
    set_default_branch(migrated_engine, 101, None)
    results = [r for r in evaluate(migrated_engine, fresh, 101) if r.policy.name == "a-optional"]
    assert rule(results, BRANCH).outcome == "unknown"


def test_every_effective_policy_is_evaluated_in_name_order(
    migrated_engine: Engine, fresh: int
) -> None:
    with Session(migrated_engine) as db:
        group = Target(target_type="group", target_id=10, path="platform", state="active")
        assign(db, fresh, save_version(db, PRIVATE_ONLY).policy, group)
        db.commit()

    results = evaluate(migrated_engine, fresh, 101)

    assert outcomes(results) == [
        ("a-private-only", VISIBILITY, "fail", ()),
        ("a-private-only", BRANCH, "fail", ()),
        ("default-gitlab-baseline", VISIBILITY, "fail", ()),
        ("default-gitlab-baseline", BRANCH, "fail", ()),
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
        assert [
            result[2:] for result in outcomes(evaluate(migrated_engine, instance_id, gitlab_id))
        ] == [
            ("unknown", (reason,)),
            ("unknown", (reason,)),
        ]


def test_never_collected(migrated_engine: Engine, instance_id: int) -> None:
    results = evaluate(migrated_engine, instance_id, 100)

    assert [result.unknown_reasons for result in results] == [("collection_never",)] * 2


def test_not_configured(migrated_engine: Engine, instance_id: int) -> None:
    result = rule(evaluate(migrated_engine, instance_id, 100, settings=None), VISIBILITY)

    assert result.unknown_reasons == ("collection_not_configured",)
    assert result.collection.status == "not_configured"


def test_a_partial_collection_decides_confirmed_projects(
    migrated_engine: Engine, instance_id: int
) -> None:
    add_run(migrated_engine, instance_id, "projects", "partial", timedelta(minutes=5))

    assert [r[2] for r in outcomes(evaluate(migrated_engine, instance_id, 100))] == ["pass"] * 2
    assert outcomes(evaluate(migrated_engine, instance_id, 104))[0][2:] == (
        "unknown",
        ("target_collection_error",),
    )


def test_other_collections_do_not_affect_visibility(migrated_engine: Engine, fresh: int) -> None:
    add_run(migrated_engine, fresh, "groups", "failed", timedelta(minutes=1))
    add_run(migrated_engine, fresh, "capabilities", "failed", timedelta(minutes=1))

    assert [r[2] for r in outcomes(evaluate(migrated_engine, fresh, 100))] == ["pass"] * 2


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
