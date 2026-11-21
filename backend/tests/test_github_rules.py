"""Shared rule types judged on GitHub repositories (roadmap M13.3).

GitHub is scripted (``test_github.FakeGitHub``); nothing here reaches github.com.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_connections import environment as environment  # the fixture
from test_github import ORG, add, collect, github, repo

from baluarte.db.models import Evaluation, Finding, GitLabInstance, GitLabProject
from baluarte.jobs.worker import Worker
from baluarte.policy.assignments import Target, assign
from baluarte.policy.evaluation.project import providers_of
from baluarte.policy.evaluation.runner import EVALUATE, evaluate
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

BASELINE = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: shared-baseline
spec:
  scope:
    groups: ["*"]
  rules:
    - id: private-only
      type: gitlab.project.visibility
      allowed: [private, internal]
    - id: protected
      type: gitlab.branch.default_protected
      required: true
    - id: two-reviews
      type: gitlab.project.merge_request_approvals
      min_approvals: 2
      forbid_author_approval: true
      reset_approvals_on_push: true
    - id: no-override
      type: gitlab.project.merge_request_approvals
      min_approvals: 1
      forbid_override: true
    - id: two-factor
      type: gitlab.group.two_factor_required
      max_grace_hours: 24
    - id: few-maintainers
      type: gitlab.project.privileged_members
      max_privileged: 5
"""


def upgrade(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403, json={"message": "Upgrade to GitHub Pro or make this repository public."}
    )


def organization(**overrides: Any) -> Any:
    return github(
        **{
            "GET /orgs/acme/repos": [
                repo(1, "api"),
                repo(2, "web", visibility="public"),
                repo(3, "legacy"),
            ],
            # api: classic protection with two reviews that a push dismisses.
            "GET /repos/acme/api/branches/main/protection": {
                "required_pull_request_reviews": {
                    "required_approving_review_count": 2,
                    "dismiss_stale_reviews": True,
                },
                "enforce_admins": {"enabled": True},
            },
            # web: no classic protection (404), one ruleset pull_request rule.
            "GET /repos/acme/web/rules/branches/main": [
                {
                    "type": "pull_request",
                    "ruleset_id": 9,
                    "ruleset_source_type": "Organization",
                    "ruleset_source": "acme",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "dismiss_stale_reviews_on_push": False,
                        "bypass_actors": ["not kept"],
                    },
                }
            ],
            # legacy: private on a plan without branch protection.
            "GET /repos/acme/legacy/branches/main/protection": upgrade,
            "GET /repos/acme/legacy/rules/branches/main": [],
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


def test_one_baseline_is_judged_on_github_repositories(
    migrated_engine: Engine, environment: None
) -> None:
    fake = organization()
    connection_id = collected_and_evaluated(migrated_engine, fake)
    assert {r.method for r in fake.requests} == {"GET"}
    found = outcomes(migrated_engine, connection_id)
    table = {key: row.outcome for key, row in found.items()}
    assert table == {
        ("acme/api", "private-only"): "pass",
        ("acme/api", "protected"): "pass",
        ("acme/api", "two-reviews"): "pass",
        ("acme/api", "no-override"): "unsupported",
        ("acme/api", "two-factor"): "pass",
        ("acme/api", "few-maintainers"): "unsupported",
        ("acme/web", "private-only"): "fail",
        ("acme/web", "protected"): "pass",
        ("acme/web", "two-reviews"): "fail",
        ("acme/web", "no-override"): "unsupported",
        ("acme/web", "two-factor"): "pass",
        ("acme/web", "few-maintainers"): "unsupported",
        ("acme/legacy", "private-only"): "pass",
        ("acme/legacy", "protected"): "unsupported",
        ("acme/legacy", "two-reviews"): "unsupported",
        ("acme/legacy", "no-override"): "unsupported",
        ("acme/legacy", "two-factor"): "pass",
        ("acme/legacy", "few-maintainers"): "unsupported",
    }
    # Every pass and fail rests on a stored GitHub observation.
    for row in found.values():
        if row.outcome in ("pass", "fail"):
            assert row.observation_id is not None, (row.rule_id, row.reason)
    web_reviews = found[("acme/web", "two-reviews")]
    assert "need 1 approving review, fewer than the 2" in web_reviews.reason
    assert "does not dismiss" in web_reviews.reason
    protected = found[("acme/web", "protected")]
    assert protected.observed["rules"] == [{"name": "ruleset rule pull_request"}]
    assert "bypass_actors" not in str(protected.observed)
    assert "no setting equivalent" in found[("acme/api", "no-override")].reason
    assert "plan" in found[("acme/legacy", "protected")].reason
    assert "on GitHub repositories" in found[("acme/api", "few-maintainers")].reason
    with Session(migrated_engine) as db:
        opened = {
            (row.rule_id, row.state)
            for row in db.scalars(select(Finding).where(Finding.state == "open"))
        }
    assert opened == {("private-only", "open"), ("two-reviews", "open")}


def test_unreported_or_unreadable_settings_are_unknown(
    migrated_engine: Engine, environment: None
) -> None:
    owner_only = {
        key: value for key, value in ORG.items() if key != "two_factor_requirement_enabled"
    }
    fake = organization(
        **{
            "GET /orgs/acme": owner_only,
            "GET /repos/acme/api/branches/main/protection": lambda _: httpx.Response(
                403, json={"message": "Resource not accessible by integration"}
            ),
        }
    )
    connection_id = collected_and_evaluated(migrated_engine, fake)
    found = outcomes(migrated_engine, connection_id)
    assert found[("acme/api", "protected")].outcome == "unknown"
    # The sampled repository refused the read, so capability detection could not confirm it.
    assert found[("acme/api", "protected")].reason == (
        "Whether this GitHub organization supports branch_protection.read has not been detected."
    )
    assert found[("acme/web", "two-factor")].outcome == "unknown"
    # Detection could not confirm the read, so no repository's branch rules are decided.
    assert found[("acme/web", "protected")].outcome == "unknown"
    assert found[("acme/web", "private-only")].outcome == "fail"


def test_rule_types_declare_their_providers() -> None:
    assert providers_of("gitlab.project.visibility") == ("gitlab", "github")
    assert providers_of("gitlab.branch.default_protected") == ("gitlab", "github")
    assert providers_of("gitlab.project.privileged_members") == ("gitlab",)
    assert providers_of("gitlab.security_policy.approval") == ("gitlab",)


def test_rule_coverage_marks_rule_types_github_does_not_judge(
    migrated_engine: Engine, environment: None
) -> None:
    from baluarte.policy.coverage import coverage

    connection_id = collected_and_evaluated(migrated_engine, organization())
    with Session(migrated_engine) as db:
        report = coverage(db, db.get_one(GitLabInstance, connection_id))
    supported = {item.rule_type: item.supported for item in report.rule_types}
    assert supported["gitlab.project.visibility"] is True
    assert supported["gitlab.branch.default_protected"] is True
    assert supported["gitlab.project.privileged_members"] is False
    assert supported["gitlab.change.reviewed"] is False
