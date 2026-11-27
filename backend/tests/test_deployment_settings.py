# ruff: noqa: F811 (pytest fixtures imported from other test modules)
"""Whoever starts a pipeline may not approve its deployments (RG.5): the rule, the template
setting, drift, change sets and applying them."""

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from test_evaluation_project import SETTINGS, add_run, fresh, instance_id  # noqa: F401
from test_template_change_execution import (  # noqa: F401
    SETS,
    GitLab,
    approved,
    as_admin,
    no_network,
    run,
    secrets,
    writes_on,
)
from test_template_change_sets import API, SITE
from test_template_drift import (  # noqa: F401
    client,
    drift,
    gitlab_env,
    observe_approvals,
    project_id,
    user,
    world,
)

from baluarte.auth.roles import Role
from baluarte.db.models import Evaluation, GitLabInstance, GitLabProject, Observation
from baluarte.evidence.observations import Observed, record
from baluarte.integrations.gitlab.release_protections import RELEASE_PROTECTION_SOURCE
from baluarte.policy.assignments import Target, assign
from baluarte.policy.evaluation.runner import evaluate_all
from baluarte.policy.loader import parse_policy
from baluarte.policy.store import save_version
from baluarte.templates import change_sets
from baluarte.templates.execute import ROLLBACK_CHANGE_SET

pytestmark = pytest.mark.db

KEY = "deployments.forbid_pipeline_triggerer_approval"
SETTING = "allow_pipeline_trigger_approve_deployment"
FORBID = {"deployments": {"forbid_pipeline_triggerer_approval": True}}
BASELINE = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: deployments
spec:
  scope:
    groups: ["*"]
  rules:
    - id: triggerer
      type: gitlab.deployment.triggerer_cannot_approve
"""


def observe(engine: Engine, gitlab_id: int, part: dict[str, Any], status: str = "ok") -> None:
    """A release protections observation whose pipeline-trigger part is ``part``."""
    with Session(engine) as db:
        project = db.scalars(
            select(GitLabProject).where(GitLabProject.gitlab_id == gitlab_id)
        ).one()
        record(
            db,
            project.instance_id,
            "project_id",
            [
                Observed(
                    target_id=project.id,
                    source=RELEASE_PROTECTION_SOURCE,
                    source_ref=f"/projects/{gitlab_id}",
                    collection_status=status,
                    payload={
                        "protected_tags": {"status": "ok", "items": []},
                        "pipeline_trigger_approval": part,
                    },
                )
            ],
        )
        db.commit()


def allowed(value: bool | None) -> dict[str, Any]:
    return {"status": "ok", "allowed": value}


@pytest.fixture
def changes_fresh(migrated_engine: Engine, fresh: int) -> int:
    add_run(migrated_engine, fresh, "changes", "succeeded", timedelta(minutes=5))
    return fresh


def evaluated(engine: Engine, instance: int) -> dict[str, Evaluation]:
    with Session(engine) as db:
        root = Target(
            target_type="instance",
            target_id=instance,
            path=db.get_one(GitLabInstance, instance).base_url,
            state=None,
        )
        assign(db, instance, save_version(db, BASELINE).policy, root)
        db.commit()
    run_id, _ = evaluate_all(engine, SETTINGS)
    with Session(engine) as db:
        rows = db.scalars(
            select(Evaluation).where(
                Evaluation.evaluation_run_id == run_id, Evaluation.rule_id == "triggerer"
            )
        ).all()
        return {db.get_one(GitLabProject, row.project_id).path_with_namespace: row for row in rows}


def test_the_rule_judges_the_setting_and_rests_on_its_evidence(
    migrated_engine: Engine, changes_fresh: int
) -> None:
    observe(migrated_engine, 100, allowed(False))
    observe(migrated_engine, 101, allowed(True))
    # Another part unread: the observation is partial, but this part is still evidence.
    observe(migrated_engine, 102, allowed(False), status="partial")
    observe(migrated_engine, 106, {"status": "forbidden", "error": "403 Forbidden"}, "partial")

    results = evaluated(migrated_engine, changes_fresh)

    assert results["platform/api"].outcome == "pass", results["platform/api"].reason
    assert results["platform/site"].outcome == "fail"
    assert "tag's creator can approve their own release" in (results["platform/site"].reason or "")
    assert results["platform/legacy"].outcome == "pass"
    assert results["bob/notes"].outcome == "unknown"
    assert "could not be read (forbidden): 403 Forbidden" in (results["bob/notes"].reason or "")
    with Session(migrated_engine) as db:
        backing = db.get_one(Observation, results["platform/api"].observation_id)
    assert backing.source == RELEASE_PROTECTION_SOURCE


def test_unread_protections_are_unknown_and_a_missing_setting_unsupported(
    migrated_engine: Engine, changes_fresh: int
) -> None:
    # GitLab answered without the setting: the plan has no deployment approvals.
    observe(migrated_engine, 106, allowed(None))

    results = evaluated(migrated_engine, changes_fresh)

    assert results["platform/api"].outcome == "unknown"
    assert "have not been read yet" in (results["platform/api"].reason or "")
    assert results["bob/notes"].outcome == "unsupported"
    assert "Premium or Ultimate" in (results["bob/notes"].reason or "")


def test_tampered_evidence_is_never_a_result(migrated_engine: Engine, changes_fresh: int) -> None:
    observe(migrated_engine, 101, allowed(True))
    with migrated_engine.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text(
                "UPDATE observation SET payload = jsonb_set(payload, "
                "'{pipeline_trigger_approval,allowed}', 'false') WHERE source = :source"
            ),
            {"source": RELEASE_PROTECTION_SOURCE},
        )

    result = evaluated(migrated_engine, changes_fresh)["platform/site"]

    assert result.outcome == "unknown"
    assert result.unknown_reasons == ["evidence_digest_mismatch"]


# --- the template setting ---


def assign_template(engine: Engine, instance: int, settings: str) -> None:
    from baluarte.templates.store import assign as assign_template_to
    from baluarte.templates.store import save_version as save_template

    source = (
        "apiVersion: baluarte.io/v1alpha1\nkind: ConfigurationTemplate\nmetadata:\n"
        f"  name: releases\nspec:\n  settings:\n{settings}"
    )
    with Session(engine) as db:
        found = db.get_one(GitLabInstance, instance)
        template = save_template(db, source, "test").template
        root = Target(target_type="instance", target_id=instance, path=found.base_url, state=None)
        assign_template_to(db, found, template, root, None, None, "test")
        db.commit()


def test_drift_from_the_release_protections(migrated_engine: Engine, changes_fresh: int) -> None:
    assign_template(
        migrated_engine,
        changes_fresh,
        "    deployments:\n      forbid_pipeline_triggerer_approval: true\n",
    )
    observe(migrated_engine, 100, allowed(False))
    observe(migrated_engine, 101, allowed(True))

    api = drift(migrated_engine, 100)["settings"]
    site = drift(migrated_engine, 101)["settings"]
    legacy = drift(migrated_engine, 102)["settings"]

    assert [(s["key"], s["status"]) for s in api] == [(KEY, "aligned")]
    assert [(s["key"], s["status"]) for s in site] == [(KEY, "drift")]
    assert site[0]["rule_type"] == "gitlab.deployment.triggerer_cannot_approve"
    assert [(s["key"], s["status"]) for s in legacy] == [(KEY, "unknown")]


def test_a_change_set_forbids_it(migrated_engine: Engine, world: int) -> None:
    add_run(migrated_engine, world, "changes", "succeeded", timedelta(minutes=5))
    observe(migrated_engine, 100, allowed(False))
    observe(migrated_engine, 101, allowed(True))
    ana = user(migrated_engine, "ana")
    analyst = client(migrated_engine, ana, Role.ANALYST)

    created = analyst.post(
        SETS,
        json={
            "title": "Separate approvers",
            "rationale": "Align the platform projects with the service template.",
            "selection": {"projects": [API, SITE]},
            "settings": FORBID,
        },
    )
    assert created.status_code == 201, created.text
    targets = {t["project"]["path"]: t for t in created.json()["document"]["targets"]}
    (change,) = targets[SITE]["changes"]
    assert (change["key"], change["before"], change["after"]) == (KEY, False, True)
    assert [w["code"] for w in change["warnings"]] == ["disrupts_work"]
    assert targets[API]["unchanged"] == [KEY]


def test_a_baseline_requiring_it_blocks_allowing_it() -> None:
    rule = parse_policy(BASELINE).document.spec.rules[0]
    baselines = [change_sets._Baseline("deployments@1", rule)]

    assert change_sets._conflict(baselines, KEY, True) is None
    assert change_sets._conflict(baselines, KEY, False) == (
        "Baseline deployments@1 rule triggerer forbids the pipeline's starter approving its "
        "deployments."
    )


class Deployments(GitLab):
    """The fake GitLab, with the project setting kept as a boolean like GitLab's."""

    def __init__(self) -> None:
        super().__init__()
        self.projects[100][SETTING] = False
        self.projects[101][SETTING] = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        for project in self.projects.values():
            if isinstance(project.get(SETTING), str):
                project[SETTING] = project[SETTING] == "true"
        return response


def test_applying_writes_the_project_setting_and_rolls_it_back(
    migrated_engine: Engine, world: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_run(migrated_engine, world, "changes", "succeeded", timedelta(minutes=5))
    observe(migrated_engine, 100, allowed(False))
    observe(migrated_engine, 101, allowed(True))
    gitlab = Deployments()
    body = approved(migrated_engine, FORBID, [API, SITE])
    admin = as_admin(migrated_engine, user(migrated_engine, "dee"), gitlab)
    writes_on(monkeypatch, admin)
    path = f"{SETS}/{body['id']}"

    assert admin.post(f"{path}/apply", json={"stage_targets": 1}).status_code == 200
    run(migrated_engine, gitlab)

    done = admin.get(path).json()
    assert [(i["project_path"], i["setting"], i["status"]) for i in done["items"]] == [
        (SITE, KEY, "applied")
    ]
    assert gitlab.projects[101][SETTING] is False
    assert [(m, p, f) for m, p, _, f in gitlab.writes()] == [
        ("PUT", "/projects/101", {SETTING: "false"})
    ]

    assert admin.post(f"{path}/rollback", json={}).status_code == 200
    run(migrated_engine, gitlab, ROLLBACK_CHANGE_SET)
    assert gitlab.projects[101][SETTING] is True
