"""The GitLab security policy rules: evaluators, evidence and the evaluation job.

Policy YAML below has the shape GitLab returns for the policies on the UAT instance: the
corporate scan execution and peer approval policies, and a license approval policy.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.orm import Session
from test_evaluation_store import World
from test_findings_api import get

from baluarte.db.models import CollectionRun, GitLabInstance, Observation
from baluarte.evidence.observations import Observed, record
from baluarte.integrations.gitlab.security_policies import PROJECT_SOURCE, READ_PROBE
from baluarte.policy.evaluation.engine import (
    EvaluationContext,
    ProjectFacts,
    SecurityPolicies,
    evaluate_rule,
)
from baluarte.policy.evaluation.model import (
    UNKNOWN_PRECEDENCE,
    CapabilityStatus,
    CollectionContext,
    PolicyRef,
    TargetRef,
)
from baluarte.policy.evaluation.project import EVALUATORS
from baluarte.policy.evaluation.security_policy import (
    APPROVAL_POLICY,
    SCAN_EXECUTION_POLICY,
    summarize,
)
from baluarte.policy.loader import PolicyInvalid, parse_policy
from baluarte.policy.schema import (
    Rule,
    SecurityPolicyApprovalRule,
    SecurityPolicyScanExecutionRule,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
SEEN = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
READ = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
POLICY = PolicyRef(policy_id=7, name="security-baseline", version=1, yaml_digest="ab" * 32)
PATH = "finance/ledger"
TARGET = TargetRef(target_type="project", gitlab_id=100, path=PATH)

SCAN_YAML = """---
name: Scan policy
description: ''
enabled: true
policy_scope:
  projects:
    excluding: []
rules:
- type: pipeline
  branch_type: default
- type: pipeline
  branch_type: target_default
  pipeline_sources:
    including:
    - merge_request_event
actions:
- scan: secret_detection
  template: latest
  variables:
    SECURE_ENABLE_LOCAL_CONFIGURATION: 'false'
- scan: sast
  template: latest
- scan: sast_iac
  template: latest
- scan: dependency_scanning
  template: latest
skip_ci:
  allowed: true
"""

PEER_YAML = """---
name: Peer approval
description: Every merge request into a protected branch needs one approval.
enabled: true
policy_scope:
  projects:
    excluding: []
rules:
- type: any_merge_request
  branch_type: protected
  commits: any
actions:
- type: require_approval
  approvals_required: 1
  role_approvers:
  - developer
  - maintainer
  - owner
approval_settings:
  prevent_approval_by_author: true
  prevent_approval_by_commit_author: true
  remove_approvals_with_new_commit: true
  block_branch_modification: true
  prevent_pushing_and_force_pushing: true
fallback_behavior:
  fail: closed
"""

LICENSE_YAML = """---
name: License Approval Policy
description: Requires approval when a dependency introduces a denied license.
enabled: true
actions:
- type: require_approval
  approvals_required: 1
  user_approvers:
  - auditor
rules:
- type: license_finding
  branches: []
  match_on_inclusion_license: true
  license_types:
  - GPL-3.0
  - unknown
  license_states:
  - newly_detected
"""

SCANS = SecurityPolicyScanExecutionRule(
    id="scanning", type="gitlab.security_policy.scan_execution", scans=["secret_detection", "sast"]
)
PEER = SecurityPolicyApprovalRule(
    id="peer-approval",
    type="gitlab.security_policy.approval",
    min_approvals=1,
    forbid_author_approval=True,
    forbid_committer_approval=True,
)
SUPPORTED: dict[str, CapabilityStatus] = {READ_PROBE: "supported"}


def policy(
    name: str,
    yaml: str | None,
    *,
    enabled: bool | None = True,
    source: str = "corporate/security-policy-project",
    excluding: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": None,
        "enabled": enabled,
        "yaml": yaml,
        "updated_at": "2026-10-23T14:06:13Z",
        "source": {"type": "project", "path": source, "inherited": None},
        "scope": {
            "compliance_frameworks": [],
            "including_projects": [],
            "excluding_projects": excluding or [],
            "including_groups": [],
            "excluding_groups": [],
            "excluding_archived_projects": False,
            "excluding_personal_projects": False,
            "truncated": False,
        },
    }


def payload(
    scan: list[dict[str, Any]] | None = None,
    approval: list[dict[str, Any]] | None = None,
    **types: dict[str, Any],
) -> dict[str, Any]:
    listing: dict[str, Any] = {
        "scan_execution": {"status": "ok", "truncated": False, "policies": scan or []},
        "approval": {"status": "ok", "truncated": False, "policies": approval or []},
        "pipeline_execution": {"status": "ok", "truncated": False, "policies": []},
        "vulnerability_management": {"status": "ok", "truncated": False, "policies": []},
    }
    listing.update(types)
    return {"security_policy_project": None, "types": listing}


CORPORATE = payload(
    scan=[policy("Scan policy", SCAN_YAML)], approval=[policy("Peer approval", PEER_YAML)]
)
LICENSE_ONLY = payload(
    approval=[
        policy("License Approval Policy", LICENSE_YAML, source="acme/platform/security-policies")
    ]
)


def read(body: dict[str, Any] | None = CORPORATE, status: str = "ok") -> SecurityPolicies:
    return SecurityPolicies(
        observation_id=1,
        status=status,
        collected_at=READ,
        payload=body,
        error=None if body is not None else "GraphQL returned 403",
    )


PROJECT = ProjectFacts(
    gitlab_id=100,
    path=PATH,
    visibility="private",
    state="active",
    last_seen_at=SEEN,
    last_error=None,
    default_branch="main",
    security_policies=read(),
)


def context(
    project: ProjectFacts | None = PROJECT,
    capabilities: dict[str, CapabilityStatus] | None = None,
    ancestry: Any = "complete",
) -> EvaluationContext:
    return EvaluationContext(
        target=TARGET,
        project=project,
        collections={
            "security_policies": CollectionContext(
                kind="security_policies", status="fresh", last_success_at=READ
            )
        },
        ancestry=ancestry,
        evaluated_at=NOW,
        capabilities=SUPPORTED if capabilities is None else capabilities,
    )


def judge(rule: Rule, body: dict[str, Any] | None = CORPORATE, status: str = "ok") -> Any:
    evaluator = SCAN_EXECUTION_POLICY if rule.type.endswith("scan_execution") else APPROVAL_POLICY
    project = replace(PROJECT, security_policies=read(body, status))
    return evaluate_rule(evaluator, POLICY, rule, context(project))


def test_both_are_registered_for_their_rule_types() -> None:
    assert EVALUATORS["gitlab.security_policy.scan_execution"] is SCAN_EXECUTION_POLICY
    assert EVALUATORS["gitlab.security_policy.approval"] is APPROVAL_POLICY


# Schema


def baseline(rules: str) -> str:
    return f"""apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: security-baseline
spec:
  scope:
    groups: ["*"]
  rules:
{rules}"""


def test_the_contract_example_parses_with_defaults() -> None:
    parsed = parse_policy(
        baseline("""    - id: secret-and-sast-scanning
      type: gitlab.security_policy.scan_execution
      scans: [secret_detection, sast]
      severity: high
    - id: peer-approval
      type: gitlab.security_policy.approval
      min_approvals: 1
""")
    )
    scans, approval = parsed.document.spec.rules
    assert isinstance(scans, SecurityPolicyScanExecutionRule)
    assert (scans.scans, scans.allow_skip_ci, scans.severity) == (
        ["secret_detection", "sast"],
        True,
        "high",
    )
    assert isinstance(approval, SecurityPolicyApprovalRule)
    assert (
        approval.min_approvals,
        approval.forbid_author_approval,
        approval.forbid_committer_approval,
    ) == (1, False, False)


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        (
            "type: gitlab.security_policy.scan_execution\n      scans: []",
            "spec.rules[0].scans",
        ),
        (
            "type: gitlab.security_policy.scan_execution\n      scans: [sast, sast]",
            "spec.rules[0]",
        ),
        (
            "type: gitlab.security_policy.scan_execution\n      scans: [fuzzing]",
            "spec.rules[0].scans[0]",
        ),
        (
            "type: gitlab.security_policy.scan_execution\n      scans: [sast]\n"
            '      allow_skip_ci: "no"',
            "spec.rules[0].allow_skip_ci",
        ),
        (
            "type: gitlab.security_policy.approval\n      min_approvals: 0",
            "spec.rules[0].min_approvals",
        ),
        (
            "type: gitlab.security_policy.approval\n      min_approvals: 21",
            "spec.rules[0].min_approvals",
        ),
        (
            'type: gitlab.security_policy.approval\n      min_approvals: "1"',
            "spec.rules[0].min_approvals",
        ),
        ("type: gitlab.security_policy.approval", "spec.rules[0].min_approvals"),
        (
            "type: gitlab.security_policy.approval\n      min_approvals: 1\n      approvers: [a]",
            "spec.rules[0].approvers",
        ),
    ],
)
def test_invalid_rules_are_rejected(rule: str, path: str) -> None:
    with pytest.raises(PolicyInvalid) as raised:
        parse_policy(baseline(f"    - id: r\n      {rule}\n"))
    assert path in [error.path for error in raised.value.errors]


# Scan execution


@pytest.mark.parametrize(
    "scans", [["secret_detection"], ["secret_detection", "sast", "sast_iac", "dependency_scanning"]]
)
def test_scans_the_corporate_policy_runs_pass(scans: list[Any]) -> None:
    result = judge(SCANS.model_copy(update={"scans": scans}))

    assert result.outcome == "pass"
    assert result.reason == "Every required scan runs on the default branch (Scan policy)."
    assert result.observed == {
        "policies": [
            {
                "name": "Scan policy",
                "source": "corporate/security-policy-project",
                "scans": ["dependency_scanning", "sast", "sast_iac", "secret_detection"],
                "runs_on_default_branch": True,
                "skip_ci_allowed": True,
            }
        ],
        "disabled": 0,
    }
    assert result.observed_at == READ
    assert result.source == PROJECT_SOURCE
    assert (result.capability.name, result.capability.status) == (READ_PROBE, "supported")
    assert result.collection.kind == "security_policies"
    assert result.expected == {"scans": scans, "allow_skip_ci": True}


def test_a_missing_scan_fails_and_names_it() -> None:
    result = judge(SCANS.model_copy(update={"scans": ["sast", "container_scanning", "dast"]}))

    assert result.outcome == "fail"
    assert result.reason == "Missing scans: container_scanning, dast."


def test_a_policy_that_can_be_skipped_does_not_count_when_skip_ci_is_forbidden() -> None:
    result = judge(SCANS.model_copy(update={"allow_skip_ci": False}))

    assert result.outcome == "fail"
    assert result.reason == (
        "Missing scans: secret_detection, sast "
        "(not counted because [skip ci] can skip them: Scan policy)."
    )
    strict = SCAN_YAML.replace("allowed: true", "allowed: false")
    assert (
        judge(
            SCANS.model_copy(update={"allow_skip_ci": False}),
            payload(scan=[policy("Scan policy", strict)]),
        ).outcome
        == "pass"
    )


def test_without_skip_ci_a_policy_can_be_skipped_as_gitlab_defaults() -> None:
    body = payload(
        scan=[policy("Scan policy", SCAN_YAML.replace("skip_ci:\n  allowed: true\n", ""))]
    )

    assert judge(SCANS, body).observed["policies"][0]["skip_ci_allowed"] is True
    assert judge(SCANS.model_copy(update={"allow_skip_ci": False}), body).outcome == "fail"


def test_scans_only_on_merge_requests_do_not_run_on_the_default_branch() -> None:
    mr_only = SCAN_YAML.replace("- type: pipeline\n  branch_type: default\n", "")
    result = judge(SCANS, payload(scan=[policy("MR scans", mr_only)]))

    assert result.outcome == "fail"
    assert result.reason == (
        "Missing scans: secret_detection, sast (not run on the default branch: MR scans)."
    )


def test_scans_from_several_policies_add_up() -> None:
    secrets = SCAN_YAML.replace("- scan: sast\n  template: latest\n", "")
    sast = """name: SAST
enabled: true
rules:
- type: schedule
  branches: ['*']
  cadence: 0 0 * * *
actions:
- scan: sast
"""
    body = payload(
        scan=[policy("Secrets", secrets), policy("SAST", sast, source="finance/policies")]
    )

    result = judge(SCANS, body)

    assert result.outcome == "pass"
    assert result.reason == "Every required scan runs on the default branch (Secrets, SAST)."


def test_no_scan_policy_fails() -> None:
    result = judge(SCANS, payload())

    assert result.outcome == "fail"
    assert result.reason == "No enabled scan execution policy reaches this project."


def test_disabled_and_excluded_policies_do_not_count() -> None:
    disabled = payload(
        scan=[
            policy(
                "Scan policy", SCAN_YAML.replace("enabled: true", "enabled: false"), enabled=False
            )
        ]
    )
    excluded = payload(scan=[policy("Scan policy", SCAN_YAML, excluding=[PATH])])

    off = judge(SCANS, disabled)
    out = judge(SCANS, excluded)

    assert (off.outcome, off.observed) == ("fail", {"policies": [], "disabled": 1})
    assert (out.outcome, out.observed) == ("fail", {"policies": [], "disabled": 0})
    assert out.reason == "No enabled scan execution policy reaches this project."


# Approval


def test_the_corporate_peer_approval_passes() -> None:
    result = judge(PEER)

    assert result.outcome == "pass"
    assert result.reason == (
        "Approval policy Peer approval requires 1 approval(s) on every merge request to "
        "protected branches."
    )
    assert result.observed == {
        "policies": [
            {
                "name": "Peer approval",
                "source": "corporate/security-policy-project",
                "approvals_required": 1,
                "every_merge_request": True,
                "prevent_approval_by_author": True,
                "prevent_approval_by_commit_author": True,
            }
        ],
        "disabled": 0,
    }
    assert result.expected == {
        "min_approvals": 1,
        "forbid_author_approval": True,
        "forbid_committer_approval": True,
    }


def test_too_few_approvals_fail() -> None:
    result = judge(PEER.model_copy(update={"min_approvals": 2}))

    assert result.outcome == "fail"
    assert result.reason == (
        "Approval policies reach this project (Peer approval), but none that does requires at "
        "least 2 approval(s) or prevents approval by the author or prevents approval by "
        "committers."
    )


def test_missing_author_prevention_fails_only_when_required() -> None:
    lax = PEER_YAML.replace("prevent_approval_by_author: true", "prevent_approval_by_author: false")
    body = payload(approval=[policy("Peer approval", lax)])

    assert judge(PEER, body).outcome == "fail"
    assert judge(PEER.model_copy(update={"forbid_author_approval": False}), body).outcome == "pass"


def test_a_license_policy_does_not_require_approval_on_every_merge_request() -> None:
    result = judge(
        PEER.model_copy(
            update={"forbid_author_approval": False, "forbid_committer_approval": False}
        ),
        LICENSE_ONLY,
    )

    assert result.outcome == "fail"
    assert result.reason == (
        "Approval policies reach this project (License Approval Policy), but none requires "
        "approval on every merge request to protected branches."
    )
    assert result.observed["policies"][0]["every_merge_request"] is False


def test_an_empty_branch_list_covers_every_protected_branch() -> None:
    every = PEER_YAML.replace("  branch_type: protected\n", "  branches: []\n")
    named = PEER_YAML.replace("  branch_type: protected\n", "  branches: [release]\n")

    assert judge(PEER, payload(approval=[policy("Peer approval", every)])).outcome == "pass"
    assert judge(PEER, payload(approval=[policy("Peer approval", named)])).outcome == "fail"


def test_no_approval_policy_fails() -> None:
    result = judge(PEER, payload())

    assert result.outcome == "fail"
    assert result.reason == "No enabled approval policy reaches this project."


# Unknown


@pytest.mark.parametrize("rule", [SCANS, PEER])
def test_policies_never_read_are_unknown(rule: Rule) -> None:
    result = evaluate_rule(
        EVALUATORS[rule.type],
        POLICY,
        rule,
        context(replace(PROJECT, security_policies=None)),
    )

    assert (result.outcome, result.unknown_reasons) == ("unknown", ("security_policies_unread",))
    assert (
        result.reason == f"The GitLab security policies of project {PATH} have not been read yet."
    )
    assert (result.observed, result.observed_at) == (None, None)


@pytest.mark.parametrize("status", ["forbidden", "failed", "not_found"])
def test_a_failed_read_is_unknown(status: str) -> None:
    result = judge(SCANS, None, status)

    assert (result.outcome, result.unknown_reasons) == ("unknown", ("security_policies_unread",))
    assert result.reason == (
        f"The GitLab security policies of project {PATH} could not be read ({status}): "
        "GraphQL returned 403."
    )


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (
            {"status": "unsupported", "truncated": False, "policies": []},
            "security_policy_type_unsupported",
        ),
        ({"status": "unknown", "truncated": False, "policies": []}, "security_policies_unread"),
        (
            {"status": "ok", "truncated": True, "policies": [policy("Scan policy", SCAN_YAML)]},
            "security_policy_truncated",
        ),
    ],
)
def test_a_type_not_read_in_full_is_unknown_never_a_gap(entry: dict[str, Any], reason: str) -> None:
    body = payload(scan_execution=entry)

    result = judge(SCANS, body, "partial")

    assert (result.outcome, result.unknown_reasons) == ("unknown", (reason,))


def test_another_type_not_read_does_not_stop_this_one() -> None:
    body = {
        **CORPORATE,
        "types": {
            **CORPORATE["types"],
            "vulnerability_management": {
                "status": "unsupported",
                "truncated": False,
                "policies": [],
            },
        },
    }

    assert judge(SCANS, body, "partial").outcome == "pass"


@pytest.mark.parametrize(
    "yaml",
    [
        None,
        "name: [unclosed",
        "!!python/object:os.system {}",
        "base: &a {x: 1}\nother: *a\n",
        "name: a\nname: b\n",
        "- a list",
    ],
)
def test_policy_yaml_that_cannot_be_read_safely_is_unknown(yaml: str | None) -> None:
    body = payload(scan=[policy("Scan policy", SCAN_YAML), policy("Broken", yaml)])

    result = judge(SCANS, body)

    assert (result.outcome, result.unknown_reasons) == (
        "unknown",
        ("security_policy_yaml_invalid",),
    )
    assert "could not be read safely" in result.reason


def test_an_enabled_flag_that_is_not_known_is_unknown() -> None:
    body = payload(
        scan=[policy("Scan policy", SCAN_YAML.replace("enabled: true\n", ""), enabled=None)]
    )

    assert judge(SCANS, body).unknown_reasons == ("security_policy_yaml_invalid",)


def test_incomplete_ancestry_outranks_the_policies() -> None:
    result = evaluate_rule(SCAN_EXECUTION_POLICY, POLICY, SCANS, context(ancestry="incomplete"))

    assert (result.outcome, result.unknown_reasons[0]) == ("unknown", "ancestry_incomplete")


def test_an_undetected_read_capability_is_unknown() -> None:
    result = evaluate_rule(SCAN_EXECUTION_POLICY, POLICY, SCANS, context(capabilities={}))

    assert (result.outcome, result.unknown_reasons) == ("unknown", ("capability_unknown",))


def test_the_new_reasons_rank_with_the_value_reasons() -> None:
    order = UNKNOWN_PRECEDENCE
    for reason in (
        "security_policies_unread",
        "security_policy_type_unsupported",
        "security_policy_truncated",
        "security_policy_yaml_invalid",
    ):
        assert (
            order.index("ancestry_incomplete") < order.index(reason) < order.index("value_unknown")
        )  # type: ignore[arg-type]


def test_the_summary_is_rebuilt_from_the_payload_alone() -> None:
    assert (
        summarize("gitlab.security_policy.scan_execution", PATH, CORPORATE) == judge(SCANS).observed
    )
    assert summarize("gitlab.security_policy.approval", PATH, CORPORATE) == judge(PEER).observed


# Stored results and the job

SECURITY_BASELINE = baseline("""    - id: scanning
      type: gitlab.security_policy.scan_execution
      scans: [secret_detection, sast]
      severity: high
    - id: peer-approval
      type: gitlab.security_policy.approval
      min_approvals: 1
      forbid_author_approval: true
""")


class SecurityWorld(World):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        with Session(engine) as db:
            db.execute(
                update(GitLabInstance)
                .where(GitLabInstance.id == self.instance_id)
                .values(
                    capability_snapshot={
                        "status": "ok",
                        "probes": {READ_PROBE: {"status": "supported"}},
                    }
                )
            )
            finished = func.now() - timedelta(minutes=5)
            db.execute(
                insert(CollectionRun).values(
                    instance_id=self.instance_id,
                    kind="security_policies",
                    status="succeeded",
                    started_at=finished,
                    finished_at=finished,
                )
            )
            db.commit()
        self.assign(SECURITY_BASELINE)

    def policies(self, project_id: int, body: dict[str, Any] | None, status: str = "ok") -> None:
        with Session(self.engine) as db:
            record(
                db,
                self.instance_id,
                "project_id",
                [
                    Observed(
                        project_id,
                        PROJECT_SOURCE,
                        "platform/p",
                        status,
                        body,
                        None if body is not None else "GraphQL returned 403",
                    )
                ],
            )
            db.commit()


@pytest.fixture
def security(migrated_engine: Engine) -> SecurityWorld:
    return SecurityWorld(migrated_engine)


@pytest.mark.db
def test_results_are_stored_with_their_policy_evidence(security: SecurityWorld) -> None:
    project_id = security.project(1, "private")
    security.policies(project_id, LICENSE_ONLY)

    security.run()

    by_rule = {e.rule_id: e for e in security.evaluations()}
    with Session(security.engine) as db:
        observation = db.scalars(
            select(Observation).where(Observation.source == PROJECT_SOURCE)
        ).one()
    scanning, approval = by_rule["scanning"], by_rule["peer-approval"]
    assert (scanning.outcome, approval.outcome) == ("fail", "fail")
    assert scanning.observation_id == approval.observation_id == observation.id
    assert scanning.source == PROJECT_SOURCE
    assert (scanning.collection_kind, scanning.collection_status) == ("security_policies", "fresh")
    assert approval.observed["policies"][0]["name"] == "License Approval Policy"
    assert {(f.rule_id, f.state) for f in security.findings()} == {
        ("scanning", "open"),
        ("peer-approval", "open"),
    }


@pytest.mark.db
def test_a_partial_observation_backs_the_types_it_read_in_full(security: SecurityWorld) -> None:
    project_id = security.project(1, "private")
    body = {
        **CORPORATE,
        "types": {
            **CORPORATE["types"],
            "vulnerability_management": {
                "status": "unsupported",
                "truncated": False,
                "policies": [],
            },
        },
    }
    security.policies(project_id, body, "partial")

    security.run()

    assert {e.rule_id: e.outcome for e in security.evaluations()} == {
        "scanning": "pass",
        "peer-approval": "pass",
    }


@pytest.mark.db
def test_unread_policies_are_unknown_and_open_nothing(security: SecurityWorld) -> None:
    project_id = security.project(1, "private")
    security.policies(project_id, None, "forbidden")

    security.run()

    assert {(e.outcome, tuple(e.unknown_reasons)) for e in security.evaluations()} == {
        ("unknown", ("security_policies_unread",))
    }
    assert security.findings() == []


@pytest.mark.db
def test_tampered_policy_evidence_never_backs_a_result(security: SecurityWorld) -> None:
    project_id = security.project(1, "private")
    security.policies(project_id, LICENSE_ONLY)
    # Someone with database rights makes the project look covered, without a new digest.
    with security.engine.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text("UPDATE observation SET payload = CAST(:body AS jsonb) WHERE source = :source"),
            {"body": json.dumps(CORPORATE), "source": PROJECT_SOURCE},
        )

    security.run()

    assert {(e.outcome, tuple(e.unknown_reasons)) for e in security.evaluations()} == {
        ("unknown", ("evidence_digest_mismatch",))
    }


@pytest.mark.db
def test_the_summary_counts_the_new_rules(
    security: SecurityWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    security.policies(security.project(1, "private"), CORPORATE)
    security.policies(security.project(2, "private"), LICENSE_ONLY)
    security.run()

    body = get(security.engine, "/api/v1/evaluations/summary")

    (baseline_results,) = [b for b in body["baselines"] if b["name"] == "security-baseline"]
    (version,) = baseline_results["versions"]
    rules = {rule["rule_id"]: rule for rule in version["rules"]}
    for rule_id, rule_type in (
        ("scanning", "gitlab.security_policy.scan_execution"),
        ("peer-approval", "gitlab.security_policy.approval"),
    ):
        assert rules[rule_id]["rule_type"] == rule_type
        assert (rules[rule_id]["results"]["pass"], rules[rule_id]["results"]["fail"]) == (1, 1)
    assert (version["projects"]["pass"], version["projects"]["fail"]) == (1, 1)
