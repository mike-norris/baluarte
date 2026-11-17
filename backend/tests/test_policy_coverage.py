"""Rule coverage: rule types in force and controls left without automated evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from oidc_fake import make_settings
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session
from test_policy_schema import BASELINE

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.controls.catalogue import CONTROLS, DEFAULT_MAPPING
from baluarte.db.models import GitLabInstance
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.main import create_app
from baluarte.policy.schema import RULE_TYPES

pytestmark = pytest.mark.db

CSRF = {"X-CSRF-Token": "csrf"}
COVERAGE = "/api/v1/policies/coverage"
VISIBILITY = "gitlab.project.visibility"
BRANCH = "gitlab.branch.default_protected"
SCAN = "gitlab.security_policy.scan_execution"
APPROVAL = "gitlab.security_policy.approval"
TWO_FACTOR = "gitlab.group.two_factor_required"
MEMBERS = "gitlab.project.privileged_members"
WHY = "Reviewed with the auditor for the 2026 audit period."

# One rule, privileged members, in a baseline that is only ever proposed or never applied.
MEMBERS_ONLY = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: {name}
spec:
  scope:
    groups: ["platform"]
  rules:
    - id: privileged-members
      type: gitlab.project.privileged_members
      max_privileged: 5
"""


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    with Session(migrated_engine) as db:
        instance_for(db, URL)
        db.commit()
    return migrated_engine


def client(engine: Engine, user_id: int, *roles: Role) -> TestClient:
    app = create_app()
    app.dependency_overrides[database_engine] = lambda: engine
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=user_id,
        subject=f"user-{user_id}",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=user_id,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    return TestClient(app, headers=CSRF)


def save_and_apply(analyst: TestClient, source: str, name: str, version: int = 1) -> None:
    saved = analyst.post("/api/v1/policies", json={"source_yaml": source})
    assert saved.status_code in (200, 201), saved.text
    applied = analyst.post(
        "/api/v1/policy-assignments",
        json={"policy_name": name, "version": version, "target_type": "instance"},
    )
    assert applied.status_code in (200, 201), applied.text


def read(engine: Engine) -> dict[str, Any]:
    response = client(engine, 7, Role.AUDITOR).get(COVERAGE)
    assert response.status_code == 200, response.text
    return dict(response.json())


def by_type(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["rule_type"]: item for item in body["rule_types"]}


def control_keys(body: dict[str, Any]) -> set[str]:
    return {f"{item['framework']}:{item['control_id']}" for item in body["controls"]}


def mapped_controls(rule_types: set[str]) -> set[str]:
    """Controls whose default rule types are all in ``rule_types``' complement."""
    by_control: dict[str, set[str]] = {}
    for pair in DEFAULT_MAPPING:
        by_control.setdefault(f"{pair.framework}:{pair.control_id}", set()).add(pair.rule_type)
    return {key for key, types in by_control.items() if not types & rule_types}


def test_nothing_applied_leaves_every_rule_type_out_of_force(engine: Engine) -> None:
    body = read(engine)

    assert [item["rule_type"] for item in body["rule_types"]] == list(RULE_TYPES)
    assert not any(item["in_force"] for item in body["rule_types"])
    assert control_keys(body) == mapped_controls(set())
    assert body["baselines"] == []
    assert body["label_taxonomy_version"] is None
    assert body["capabilities_checked_at"] is None
    # Without a capability snapshot, only rule types that need no capability are known.
    types = by_type(body)
    assert types[VISIBILITY]["supported"] is True
    assert types[MEMBERS]["supported"] is True
    assert (types[BRANCH]["supported"], types[BRANCH]["capability"]) == (
        None,
        "protected_branches.read",
    )
    assert types[SCAN]["supported"] is None


def test_only_applied_versions_count_not_proposed_or_unapplied_ones(engine: Engine) -> None:
    analyst = client(engine, 1, Role.ANALYST)
    save_and_apply(analyst, BASELINE, "default-gitlab-baseline")
    # Saved but never applied.
    analyst.post("/api/v1/policies", json={"source_yaml": MEMBERS_ONLY.format(name="unapplied")})
    # Proposed while review is required: neither the version nor its assignment is in force.
    reviewer = client(engine, 9, Role.ADMIN)
    assert (
        reviewer.put("/api/v1/settings/governance", json={"require_review": True}).status_code
        == 200
    )
    proposed = analyst.post(
        "/api/v1/policies", json={"source_yaml": MEMBERS_ONLY.format(name="proposed")}
    )
    assert proposed.status_code == 202

    body = read(engine)

    types = by_type(body)
    assert {name for name, item in types.items() if item["in_force"]} == {VISIBILITY, BRANCH}
    assert types[VISIBILITY]["baselines"] == [{"name": "default-gitlab-baseline", "version": 1}]
    assert types[MEMBERS]["baselines"] == []
    assert control_keys(body) == mapped_controls({VISIBILITY, BRANCH})
    assert "soc2:CC6.1" not in control_keys(body)
    assert "nist_800_53_moderate:RA-5" in control_keys(body)
    ra5 = next(item for item in body["controls"] if item["control_id"] == "RA-5")
    assert ra5["title"] == next(c.title for c in CONTROLS if c.id == "RA-5")
    assert ra5["rule_types"] == [SCAN]
    assert ra5["status_change"] is None
    assert body["baselines"] == [
        {
            "name": "default-gitlab-baseline",
            "latest_version": 1,
            "applied": True,
            "rule_types": [VISIBILITY, BRANCH],
        },
        {"name": "unapplied", "latest_version": 1, "applied": False, "rule_types": [MEMBERS]},
    ]


def test_the_pinned_version_counts_not_a_newer_unapplied_one(engine: Engine) -> None:
    analyst = client(engine, 1, Role.ANALYST)
    save_and_apply(analyst, BASELINE, "default-gitlab-baseline")
    newer = BASELINE + (
        "    - id: privileged-members\n"
        "      type: gitlab.project.privileged_members\n"
        "      max_privileged: 5\n"
    )
    assert analyst.post("/api/v1/policies", json={"source_yaml": newer}).status_code == 201

    body = read(engine)

    assert by_type(body)[MEMBERS]["in_force"] is False
    assert body["baselines"][0]["latest_version"] == 2
    assert MEMBERS in body["baselines"][0]["rule_types"]

    applied = analyst.post(
        "/api/v1/policy-assignments",
        json={"policy_name": "default-gitlab-baseline", "version": 2, "target_type": "instance"},
    )
    assert applied.status_code == 201
    assert by_type(read(engine))[MEMBERS]["baselines"] == [
        {"name": "default-gitlab-baseline", "version": 2}
    ]


def test_controls_follow_the_mapping_in_force(engine: Engine) -> None:
    analyst = client(engine, 1, Role.ANALYST)
    save_and_apply(analyst, BASELINE, "default-gitlab-baseline")
    assert "nist_800_53_moderate:RA-5" in control_keys(read(engine))

    mapped = analyst.post(
        "/api/v1/control-mapping/changes",
        json={
            "framework": "nist_800_53_moderate",
            "control_id": "RA-5",
            "kind": "map",
            "rule_type": VISIBILITY,
            "justification": WHY,
        },
    )
    assert mapped.status_code == 201, mapped.text
    attested = analyst.post(
        "/api/v1/control-mapping/changes",
        json={
            "framework": "nist_800_53_moderate",
            "control_id": "SI-2",
            "kind": "manual",
            "justification": WHY,
            "review_by": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "evidence_ref": "Quarterly patch report in the audit folder.",
        },
    )
    assert attested.status_code == 201, attested.text

    body = read(engine)

    assert "nist_800_53_moderate:RA-5" not in control_keys(body)
    si2 = next(item for item in body["controls"] if item["control_id"] == "SI-2")
    assert si2["status_change"] == "manual"


def test_supported_follows_the_capability_snapshot(engine: Engine) -> None:
    snapshot = {
        "status": "partial",
        "checked_at": "2026-11-01T10:00:00+00:00",
        "version": "17.5.0",
        "edition": "ee",
        "tier": None,
        "error": None,
        "probes": {
            "protected_branches.read": {"status": "supported", "endpoint": "x"},
            "security_policies.read": {"status": "unsupported", "endpoint": "x"},
            "groups.two_factor": {"status": "forbidden", "endpoint": "x"},
        },
    }
    with Session(engine) as db:
        db.execute(update(GitLabInstance).values(capability_snapshot=snapshot))
        db.commit()

    body = read(engine)

    types = by_type(body)
    assert types[BRANCH]["supported"] is True
    assert types[SCAN]["supported"] is False
    assert types[APPROVAL]["supported"] is False
    # A refusal does not prove GitLab lacks the feature.
    assert types[TWO_FACTOR]["supported"] is None
    assert body["capabilities_checked_at"].startswith("2026-11-01T10:00:00")

    with Session(engine) as db:
        db.execute(
            update(GitLabInstance).values(capability_snapshot={**snapshot, "status": "failed"})
        )
        db.commit()
    assert by_type(read(engine))[BRANCH]["supported"] is None


@pytest.mark.parametrize("roles", [(Role.AUDITOR,), (Role.ANALYST,), (Role.MANAGER,)])
def test_every_reader_can_read_coverage(engine: Engine, roles: tuple[Role, ...]) -> None:
    assert client(engine, 3, *roles).get(COVERAGE).status_code == 200


def test_reading_coverage_needs_findings_read(engine: Engine) -> None:
    assert client(engine, 3).get(COVERAGE).status_code == 403
