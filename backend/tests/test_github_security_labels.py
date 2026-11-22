"""GitHub security features and label drift (roadmap M13.3b).

GitHub is scripted (``test_github.FakeGitHub``); nothing here reaches github.com.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_connections import client_as
from test_connections import environment as environment  # the fixture
from test_exceptions import user
from test_github import add, collect, github, repo

from baluarte.api.v1 import labels as labels_api
from baluarte.auth.roles import Role
from baluarte.db.models import CollectionRun, Evaluation, GitLabInstance, GitLabProject
from baluarte.integrations.github.repositories import LABELS_SOURCE, SECURITY_SOURCE
from baluarte.jobs.worker import Worker
from baluarte.labels import taxonomy
from baluarte.policy.assignments import Target, assign
from baluarte.policy.changes import Actor
from baluarte.policy.evaluation.project import PROVIDER_EVALUATORS, providers_of
from baluarte.policy.evaluation.runner import EVALUATE, evaluate
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

BASELINE = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: github-security
spec:
  scope:
    groups: ["*"]
  rules:
    - id: scanning
      type: github.repository.security_features
      features: [secret_scanning, secret_scanning_push_protection, dependabot_security_updates]
    - id: code-scanning
      type: github.repository.security_features
      features: [code_scanning]
    - id: labels
      type: gitlab.labels.consistent
"""

TAXONOMY = """\
apiVersion: baluarte.io/v1alpha1
kind: LabelTaxonomy
metadata:
  name: acme-labels
spec:
  scope:
    groups: ["acme"]
  labels:
    - name: bug
      color: "#D73A4A"
    - name: docs
      color: "#0075CA"
      description: "Documentation"
"""


def security(**statuses: str) -> dict[str, Any]:
    return {name: {"status": status} for name, status in statuses.items()}


def no_ghas(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "message": "Advanced Security must be enabled for this repository to use code scanning."
        },
    )


def no_analysis(_: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"message": "no analysis found"})


def organization(**overrides: Any) -> Any:
    on = security(
        secret_scanning="enabled",
        secret_scanning_push_protection="enabled",
        dependabot_security_updates="enabled",
    )
    return github(
        **{
            "GET /orgs/acme/repos": [
                repo(1, "api"),
                repo(2, "web", visibility="public"),
                repo(3, "legacy"),
            ],
            "GET /repos/acme/legacy/rules/branches/main": [],
            # api: everything on, and an analysis of the default branch.
            "GET /repos/acme/api": repo(1, "api", security_and_analysis=on),
            "GET /repos/acme/api/code-scanning/analyses": [{"id": 7, "ref": "refs/heads/main"}],
            "GET /repos/acme/api/labels": [
                {"id": 1, "name": "bug", "color": "d73a4a", "description": None},
                {"id": 2, "name": "docs", "color": "0075ca", "description": "Documentation"},
            ],
            # web: push protection off, no analysis yet; a label of the wrong colour.
            "GET /repos/acme/web": repo(
                2,
                "web",
                visibility="public",
                security_and_analysis=security(
                    secret_scanning="enabled",
                    secret_scanning_push_protection="disabled",
                    dependabot_security_updates="enabled",
                ),
            ),
            "GET /repos/acme/web/code-scanning/analyses": no_analysis,
            "GET /repos/acme/web/labels": [
                {"id": 3, "name": "bug", "color": "ffffff", "description": None},
                {"id": 4, "name": "docs", "color": "0075ca", "description": "Documentation"},
            ],
            # legacy: settings hidden from a token without admin rights; no GHAS licence.
            "GET /repos/acme/legacy": repo(3, "legacy"),
            "GET /repos/acme/legacy/code-scanning/analyses": no_ghas,
            "GET /repos/acme/legacy/labels": lambda _: httpx.Response(
                403, json={"message": "Resource not accessible"}
            ),
            **overrides,
        }
    )


def collected_and_evaluated(engine: Engine, fake: Any) -> int:
    connection_id = int(add(engine).json()["id"])
    by = user(engine, "ana")
    with Session(engine) as db:
        instance = db.get_one(GitLabInstance, connection_id)
        taxonomy.propose(db, instance.id, TAXONOMY, None, Actor(by, f"user:{by}"))
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


def outcomes(engine: Engine, connection_id: int) -> dict[tuple[str, str], Evaluation]:
    with Session(engine) as db:
        rows = db.execute(
            select(GitLabProject.path_with_namespace, Evaluation)
            .join(GitLabProject, GitLabProject.id == Evaluation.project_id)
            .where(Evaluation.instance_id == connection_id)
        ).all()
        for _, row in rows:
            db.expunge(row)
        return {(path, row.rule_id): row for path, row in rows}


def test_security_features_and_labels_are_judged_on_github(
    migrated_engine: Engine, environment: None
) -> None:
    fake = organization()
    connection_id = collected_and_evaluated(migrated_engine, fake)
    assert {r.method for r in fake.requests} == {"GET"}
    found = outcomes(migrated_engine, connection_id)
    assert {key: row.outcome for key, row in found.items()} == {
        ("acme/api", "scanning"): "pass",
        ("acme/api", "code-scanning"): "pass",
        ("acme/api", "labels"): "pass",
        ("acme/web", "scanning"): "fail",
        ("acme/web", "code-scanning"): "fail",
        ("acme/web", "labels"): "fail",
        ("acme/legacy", "scanning"): "unknown",
        ("acme/legacy", "code-scanning"): "unsupported",
        ("acme/legacy", "labels"): "unknown",
    }
    for row in found.values():
        if row.outcome in ("pass", "fail"):
            assert row.observation_id is not None, (row.rule_id, row.reason)
    assert found[("acme/web", "scanning")].reason == (
        "Not enabled: secret scanning push protection."
    )
    assert "admin rights" in found[("acme/legacy", "scanning")].reason
    assert "Advanced Security" in found[("acme/legacy", "code-scanning")].reason
    assert "acme/legacy" in found[("acme/legacy", "labels")].reason
    with Session(migrated_engine) as db:
        runs = {
            row.kind: (row.status, row.counts)
            for row in db.scalars(
                select(CollectionRun).where(CollectionRun.instance_id == connection_id)
            )
        }
    assert runs["projects"][0] == "succeeded"
    assert runs["projects"][1]["security"] == {"ok": 3}
    # legacy's labels were refused: the labels run is partial, never "no labels".
    assert runs["labels"][0] == "partial"
    assert runs["labels"][1] == {"ok": 2, "forbidden": 1}


def test_unread_security_settings_are_unknown(migrated_engine: Engine, environment: None) -> None:
    fake = organization(
        **{
            "GET /repos/acme/api": lambda _: httpx.Response(500, json={"message": "boom"}),
        }
    )
    connection_id = collected_and_evaluated(migrated_engine, fake)
    found = outcomes(migrated_engine, connection_id)
    assert found[("acme/api", "scanning")].outcome == "unknown"
    assert "could not be read" in found[("acme/api", "scanning")].reason
    with Session(migrated_engine) as db:
        run = db.scalars(
            select(CollectionRun).where(
                CollectionRun.instance_id == connection_id, CollectionRun.kind == "projects"
            )
        ).one()
    assert run.status == "partial"
    assert "security features of 1 repositories" in (run.error or "")


def test_sources_and_providers() -> None:
    assert SECURITY_SOURCE != LABELS_SOURCE
    assert providers_of("github.repository.security_features") == ("github",)
    assert providers_of("gitlab.labels.consistent") == ("gitlab", "github")
    gitlab_only = PROVIDER_EVALUATORS["gitlab"]["github.repository.security_features"]
    assert gitlab_only.unsupported(None, None) == (  # type: ignore[arg-type]
        "Baluarte does not evaluate github.repository.security_features on GitLab projects."
    )
    assert set(PROVIDER_EVALUATORS["gitlab"]) == set(PROVIDER_EVALUATORS["github"])


def test_label_plans_are_refused_on_github(
    migrated_engine: Engine, environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = int(add(migrated_engine).json()["id"])
    with Session(migrated_engine) as db:
        instance = db.get_one(GitLabInstance, connection_id)
        db.expunge(instance)
    # The request works on the GitHub connection (as in a workspace that holds only it).
    monkeypatch.setattr(labels_api, "_instance", lambda _db: instance)
    http = client_as(migrated_engine, Role.ADMIN)
    refused = http.post("/api/v1/labels/plans", json={}, headers={"X-CSRF-Token": "csrf"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "provider_not_supported"
