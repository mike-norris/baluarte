"""The framework control mapping: catalogue, organization changes and their review, posture per
control, findings' controls and the evidence export."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from oidc_fake import make_settings
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_store import World
from test_policy_schema import BASELINE

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.compliance.catalogue import FRAMEWORK_IDS
from baluarte.controls.catalogue import CONTROLS, DEFAULT_MAPPING, MAPPING_VERSION, control
from baluarte.controls.posture import Counts, MappedRule, status_of
from baluarte.db.models import AuditEvent, ControlMappingChange
from baluarte.main import create_app
from baluarte.policy.schema import RULE_TYPES

CSRF = {"X-CSRF-Token": "csrf"}
CHANGES = "/api/v1/control-mapping/changes"
WHY = "Reviewed with the auditor for the 2026 audit period."
VISIBILITY = "gitlab.project.visibility"
BRANCH = "gitlab.branch.default_protected"


# Catalogue -----------------------------------------------------------------------------------


def test_every_default_pair_names_a_control_and_a_rule_type_once() -> None:
    pairs = [(p.rule_type, p.framework, p.control_id) for p in DEFAULT_MAPPING]

    assert len(pairs) == len(set(pairs))
    for pair in DEFAULT_MAPPING:
        assert control(pair.framework, pair.control_id) is not None, pair
        assert pair.rule_type in RULE_TYPES, pair
        assert pair.rationale.endswith("."), pair
        assert 10 < len(pair.rationale) <= 120, pair


def test_every_rule_type_supports_a_control_in_every_framework() -> None:
    for rule_type in RULE_TYPES:
        frameworks = {p.framework for p in DEFAULT_MAPPING if p.rule_type == rule_type}
        assert frameworks == set(FRAMEWORK_IDS), rule_type


def test_controls_are_unique_with_short_paraphrased_titles() -> None:
    keys = [(c.framework, c.id) for c in CONTROLS]

    assert len(keys) == len(set(keys))
    assert {c.framework for c in CONTROLS} == set(FRAMEWORK_IDS)
    assert all(0 < len(c.title) <= 70 and c.area for c in CONTROLS)
    assert MAPPING_VERSION == "2026.11.1"


def test_a_failure_outranks_manual_but_not_not_applicable() -> None:
    today = date(2026, 11, 3)
    mapped = [MappedRule(VISIBILITY, "default", None, "Why.")]
    failing = Counts(fail=1, projects={1})

    def change(kind: str, review_by: date) -> ControlMappingChange:
        return ControlMappingChange(kind=kind, review_by=review_by)

    assert status_of(mapped, failing, change("manual", today), today) == "not_met"
    assert status_of(mapped, failing, change("not_applicable", today), today) == "not_applicable"
    yesterday = today - timedelta(days=1)
    assert status_of([], Counts(), change("manual", yesterday), today) == "review_overdue"
    assert status_of([], Counts(), change("not_applicable", yesterday), today) == "review_overdue"
    assert status_of([], Counts(), None, today) == "not_covered"
    assert status_of(mapped, Counts(), None, today) == "no_results"
    assert status_of(mapped, Counts(unknown=1, projects={1}), None, today) == "unknown"
    assert status_of(mapped, Counts(passed=1, excepted=1, projects={1}), None, today) == "met"


# API -----------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


@pytest.fixture
def world(migrated_engine: Engine) -> World:
    world = World(migrated_engine)
    # Visibility and default-branch protection: the second is unknown without a protection read.
    world.assign(BASELINE)
    return world


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


def analyst(engine: Engine, user_id: int = 1) -> TestClient:
    return client(engine, user_id, Role.ANALYST)


def auditor(engine: Engine) -> TestClient:
    return client(engine, 7, Role.AUDITOR)


def require_review(engine: Engine) -> None:
    response = client(engine, 9, Role.ADMIN).put(
        "/api/v1/settings/governance", json={"require_review": True}
    )
    assert response.status_code == 200


def controls(engine: Engine, framework: str | None = None) -> dict[str, dict[str, Any]]:
    query = f"?framework={framework}" if framework else ""
    response = auditor(engine).get(f"/api/v1/controls{query}")
    assert response.status_code == 200, response.text
    return {f"{c['framework']}:{c['id']}": c for c in response.json()["controls"]}


def propose(engine: Engine, user_id: int = 1, **body: Any) -> Any:
    return analyst(engine, user_id).post(CHANGES, json={"justification": WHY, **body})


def in_days(days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def codes(response: Any) -> list[str]:
    detail = response.json()["detail"]
    assert response.status_code == 409
    assert detail["code"] == detail["reasons"][0]["code"]
    return [reason["code"] for reason in detail["reasons"]]


def actions(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.action.like("control_mapping.%"))
                .order_by(AuditEvent.id)
            )
        )


def test_controls_show_the_default_mapping_and_status_from_the_latest_run(world: World) -> None:
    world.project(1, "public")
    world.project(2, "private")
    world.run()

    response = auditor(world.engine).get("/api/v1/controls")
    body = response.json()
    found = {f"{c['framework']}:{c['id']}": c for c in body["controls"]}

    assert body["mapping_version"] == MAPPING_VERSION
    assert body["evaluation_run_id"] is not None
    assert [f["id"] for f in body["frameworks"]] == list(FRAMEWORK_IDS)
    assert len(found) == len(CONTROLS)
    access = found["soc2:CC6.6"]
    assert access["title"] == "Access from outside the system boundary is restricted"
    assert access["mapped"] == [
        {
            "rule_type": VISIBILITY,
            "source": "default",
            "change_id": None,
            "rationale": "Internal or public visibility opens the code beyond its members.",
            "justification": None,
        }
    ]
    assert access["status"] == "not_met"
    assert access["counts"] == {"pass": 1, "fail": 1, "unknown": 0, "excepted": 0, "projects": 2}
    assert access["change"] is None
    # Only default-branch protection is mapped, and it could not be read.
    assert found["nist_800_53_moderate:CM-5"]["status"] == "unknown"
    assert found["nist_800_53_moderate:CM-5"]["counts"]["unknown"] == 2
    assert found["soc2:CC6.8"]["status"] == "not_covered"
    assert found["iso27001:A.8.5"]["status"] == "no_results"
    assert set(controls(world.engine, "soc2")) == {k for k in found if k.startswith("soc2:")}


def test_a_control_is_met_when_every_result_passes(world: World) -> None:
    world.project(2, "private")
    world.run()

    assert controls(world.engine)["soc2:CC6.6"]["status"] == "met"


def test_without_a_successful_run_mapped_controls_have_no_results(migrated_engine: Engine) -> None:
    response = auditor(migrated_engine).get("/api/v1/controls")
    body = response.json()

    assert body["evaluation_run_id"] is None
    statuses = {c["status"] for c in body["controls"]}
    assert statuses == {"no_results", "not_covered"}


def test_reading_needs_findings_read_and_changing_needs_policy_write(world: World) -> None:
    nobody = client(world.engine, 3)

    assert nobody.get("/api/v1/controls").status_code == 403
    assert nobody.get(CHANGES).status_code == 403
    denied = auditor(world.engine).post(
        CHANGES,
        json={
            "framework": "soc2",
            "control_id": "CC6.8",
            "kind": "map",
            "rule_type": VISIBILITY,
            "justification": WHY,
        },
    )
    assert denied.status_code == 403
    assert auditor(world.engine).get(CHANGES).status_code == 200


def test_without_review_a_mapping_applies_at_once_as_self_approved(world: World) -> None:
    world.project(1, "public")
    world.run()

    response = propose(
        world.engine, framework="soc2", control_id="CC6.8", kind="map", rule_type=VISIBILITY
    )

    assert response.status_code == 201, response.text
    change = response.json()
    assert (change["state"], change["self_approved"], change["review_required"]) == (
        "active",
        True,
        False,
    )
    assert change["proposed_by"]["actor"] == change["decided_by"]["actor"] == "user:1"
    assert change["mapping_version"] == MAPPING_VERSION
    item = controls(world.engine)["soc2:CC6.8"]
    assert item["status"] == "not_met"
    assert item["mapped"] == [
        {
            "rule_type": VISIBILITY,
            "source": "organization",
            "change_id": change["id"],
            "rationale": None,
            "justification": WHY,
        }
    ]
    assert actions(world.engine) == ["control_mapping.proposed"]
    with Session(world.engine) as db:
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "control_mapping.proposed")
        ).one()
    assert event.details["self_approved"] is True
    assert event.details["justification"] == WHY


def test_unmapping_a_default_pair_removes_it_from_controls_and_findings(world: World) -> None:
    world.project(1, "public")
    world.run()

    response = propose(
        world.engine, framework="soc2", control_id="CC6.6", kind="unmap", rule_type=VISIBILITY
    )

    assert response.status_code == 201
    item = controls(world.engine)["soc2:CC6.6"]
    assert (item["status"], item["mapped"]) == ("not_covered", [])
    assert item["unmapped"] == [
        {"rule_type": VISIBILITY, "change_id": response.json()["id"], "justification": WHY}
    ]
    finding = next(
        f
        for f in auditor(world.engine).get("/api/v1/findings").json()["items"]
        if f["rule_id"] == "project-visibility"
    )
    assert {"framework": "soc2", "id": "CC6.1"} in finding["controls"]
    assert {"framework": "soc2", "id": "CC6.6"} not in finding["controls"]


def test_findings_list_their_controls_in_catalogue_order(world: World) -> None:
    world.project(1, "public")
    world.run()

    items = auditor(world.engine).get("/api/v1/findings").json()["items"]
    (finding,) = [f for f in items if f["rule_id"] == "project-visibility"]
    detail = auditor(world.engine).get(f"/api/v1/findings/{finding['id']}").json()

    expected = [
        {"framework": p.framework, "id": p.control_id}
        for p in sorted(
            (p for p in DEFAULT_MAPPING if p.rule_type == VISIBILITY),
            key=lambda p: [(c.framework, c.id) for c in CONTROLS].index(
                (p.framework, p.control_id)
            ),
        )
    ]
    assert finding["controls"] == expected
    assert detail["controls"] == expected


@pytest.mark.parametrize(
    ("body", "reasons"),
    [
        (
            {"framework": "soc2", "control_id": "CC9.9", "kind": "map", "rule_type": VISIBILITY},
            ["control_unknown"],
        ),
        (
            {"framework": "soc2", "control_id": "CC6.8", "kind": "map", "rule_type": "gitlab.x"},
            ["rule_type_unknown"],
        ),
        (
            {"framework": "soc2", "control_id": "CC6.6", "kind": "map", "rule_type": VISIBILITY},
            ["already_mapped"],
        ),
        (
            {"framework": "soc2", "control_id": "CC6.8", "kind": "unmap", "rule_type": VISIBILITY},
            ["not_default"],
        ),
        (
            {
                "framework": "soc2",
                "control_id": "CC6.8",
                "kind": "manual",
                "review_by": "2020-01-01",
                "evidence_ref": "Endpoint protection reports",
            },
            ["review_by_past"],
        ),
        (
            {
                "framework": "soc2",
                "control_id": "CC6.8",
                "kind": "not_applicable",
                "review_by": "2099-01-01",
            },
            ["review_by_too_far"],
        ),
        (
            {"framework": "soc2", "control_id": "CC6.8", "kind": "withdraw", "supersedes_id": 999},
            ["supersedes_not_active"],
        ),
    ],
)
def test_conflicts_are_refused_with_reason_codes(
    world: World, body: dict[str, Any], reasons: list[str]
) -> None:
    response = propose(world.engine, **body)

    assert response.status_code == 409, response.text
    assert codes(response) == reasons
    assert response.json()["detail"]["code"] == reasons[0]


def test_a_change_cannot_repeat_or_stack_on_a_control(world: World) -> None:
    mapped = propose(
        world.engine, framework="soc2", control_id="CC6.8", kind="map", rule_type=VISIBILITY
    )
    status = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.8",
        kind="not_applicable",
        review_by=in_days(90),
    )

    again = propose(
        world.engine, framework="soc2", control_id="CC6.8", kind="map", rule_type=VISIBILITY
    )
    manual = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.8",
        kind="manual",
        review_by=in_days(90),
        evidence_ref="Endpoint protection console",
    )
    elsewhere = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.1",
        kind="withdraw",
        supersedes_id=mapped.json()["id"],
    )
    renew_with_map = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.8",
        kind="manual",
        review_by=in_days(90),
        evidence_ref="Endpoint protection console",
        supersedes_id=mapped.json()["id"],
    )

    assert status.status_code == 201
    assert codes(again) == ["duplicate"]
    assert codes(manual) == ["status_exists"]
    assert codes(elsewhere) == ["supersedes_other_control"]
    assert codes(renew_with_map) == [
        "supersedes_invalid_kind",
        "status_exists",
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"framework": "soc2", "control_id": "CC6.8", "kind": "map"},
        {"framework": "soc2", "control_id": "CC6.8", "kind": "not_applicable"},
        {"framework": "soc2", "control_id": "CC6.8", "kind": "manual", "review_by": "2099-01-01"},
        {"framework": "soc2", "control_id": "CC6.8", "kind": "withdraw"},
        {
            "framework": "soc2",
            "control_id": "CC6.8",
            "kind": "map",
            "rule_type": VISIBILITY,
            "review_by": "2099-01-01",
        },
        {
            "framework": "soc2",
            "control_id": "CC6.8",
            "kind": "map",
            "rule_type": VISIBILITY,
            "supersedes_id": 1,
        },
        {
            "framework": "soc2",
            "control_id": "CC6.8",
            "kind": "map",
            "rule_type": VISIBILITY,
            "justification": "too short",
        },
        {"framework": "pci", "control_id": "CC6.8", "kind": "map", "rule_type": VISIBILITY},
        {
            "framework": "soc2",
            "control_id": "CC6.8",
            "kind": "map",
            "rule_type": VISIBILITY,
            "extra": True,
        },
    ],
)
def test_a_body_that_does_not_fit_its_kind_is_refused(world: World, body: dict[str, Any]) -> None:
    payload = {"justification": WHY, **body}

    response = analyst(world.engine).post(CHANGES, json=payload)

    assert response.status_code == 422, response.text


def test_with_review_a_change_waits_for_someone_else(world: World) -> None:
    require_review(world.engine)
    world.project(1, "public")
    world.run()

    proposed = propose(
        world.engine, framework="soc2", control_id="CC6.8", kind="map", rule_type=VISIBILITY
    ).json()
    own = analyst(world.engine, 1).post(f"{CHANGES}/{proposed['id']}/approve", json={})
    before = controls(world.engine)["soc2:CC6.8"]["status"]
    approved = analyst(world.engine, 2).post(
        f"{CHANGES}/{proposed['id']}/approve", json={"note": "Agreed"}
    )
    twice = analyst(world.engine, 2).post(f"{CHANGES}/{proposed['id']}/approve", json={})

    assert (proposed["state"], proposed["self_approved"], proposed["decided_by"]) == (
        "proposed",
        False,
        None,
    )
    assert own.status_code == 403
    assert own.json()["detail"]["code"] == "own_change"
    assert before == "not_covered"
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "active"
    assert approved.json()["decided_by"]["actor"] == "user:2"
    assert approved.json()["decision_note"] == "Agreed"
    assert controls(world.engine)["soc2:CC6.8"]["status"] == "not_met"
    assert twice.status_code == 409
    assert twice.json()["detail"]["code"] == "not_proposed"
    assert actions(world.engine) == ["control_mapping.proposed", "control_mapping.approved"]


def test_rejecting_needs_a_note_and_changes_nothing(world: World) -> None:
    require_review(world.engine)
    proposed = propose(
        world.engine, framework="soc2", control_id="CC6.6", kind="unmap", rule_type=VISIBILITY
    ).json()

    no_note = analyst(world.engine, 2).post(f"{CHANGES}/{proposed['id']}/reject", json={})
    own = analyst(world.engine, 1).post(f"{CHANGES}/{proposed['id']}/reject", json={"note": "x"})
    rejected = analyst(world.engine, 2).post(
        f"{CHANGES}/{proposed['id']}/reject", json={"note": "Keep the default."}
    )
    missing = analyst(world.engine, 2).post(f"{CHANGES}/999/reject", json={"note": "x"})

    assert no_note.status_code == 422
    assert own.status_code == 403
    assert rejected.json()["state"] == "rejected"
    assert missing.status_code == 404
    assert controls(world.engine)["soc2:CC6.6"]["mapped"][0]["source"] == "default"
    assert actions(world.engine) == ["control_mapping.proposed", "control_mapping.rejected"]


def test_approval_checks_the_change_again(world: World) -> None:
    require_review(world.engine)
    with Session(world.engine) as db:
        stale = ControlMappingChange(
            framework="soc2",
            control_id="CC6.8",
            kind="not_applicable",
            justification=WHY,
            review_by=date(2026, 1, 1),
            state="proposed",
            review_required=True,
            self_approved=False,
            mapping_version=MAPPING_VERSION,
            proposed_by="user:1",
            proposed_by_user_id=1,
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

    response = analyst(world.engine, 2).post(f"{CHANGES}/{stale_id}/approve", json={})

    assert response.status_code == 409
    assert codes(response) == ["review_by_past"]


def test_not_applicable_and_manual_controls_and_their_review_date(world: World) -> None:
    world.project(1, "public")
    world.run()

    scoped_out = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.6",
        kind="not_applicable",
        review_by=in_days(30),
    ).json()
    manual = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.1",
        kind="manual",
        review_by=in_days(30),
        evidence_ref="Access review tickets",
    ).json()
    elsewhere = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.8",
        kind="manual",
        review_by=in_days(30),
        evidence_ref="Endpoint protection console",
    ).json()

    found = controls(world.engine)
    assert found["soc2:CC6.6"]["status"] == "not_applicable"
    assert found["soc2:CC6.6"]["change"] == {
        "id": scoped_out["id"],
        "kind": "not_applicable",
        "justification": WHY,
        "evidence_ref": None,
        "review_by": in_days(30),
    }
    # A mapped rule fails, which outranks the manual record.
    assert found["soc2:CC6.1"]["status"] == "not_met"
    assert found["soc2:CC6.1"]["change"]["id"] == manual["id"]
    assert found["soc2:CC6.8"]["status"] == "manual"
    assert found["soc2:CC6.8"]["change"]["evidence_ref"] == "Endpoint protection console"
    assert elsewhere["state"] == "active"


def test_an_overdue_status_shows_until_it_is_renewed(world: World) -> None:
    with Session(world.engine) as db:
        overdue = ControlMappingChange(
            framework="soc2",
            control_id="CC6.8",
            kind="not_applicable",
            justification=WHY,
            review_by=date(2026, 1, 1),
            state="active",
            review_required=False,
            self_approved=True,
            mapping_version=MAPPING_VERSION,
            proposed_by="user:1",
            proposed_by_user_id=1,
            decided_by="user:1",
            decided_by_user_id=1,
            decided_at=datetime(2025, 12, 1, tzinfo=UTC),
        )
        db.add(overdue)
        db.commit()
        overdue_id = overdue.id

    assert controls(world.engine)["soc2:CC6.8"]["status"] == "review_overdue"

    renewed = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.8",
        kind="not_applicable",
        review_by=in_days(180),
        supersedes_id=overdue_id,
    )

    assert renewed.status_code == 201, renewed.text
    assert controls(world.engine)["soc2:CC6.8"]["status"] == "not_applicable"
    with Session(world.engine) as db:
        assert db.get_one(ControlMappingChange, overdue_id).state == "withdrawn"
    assert actions(world.engine) == ["control_mapping.proposed", "control_mapping.withdrawn"]


def test_withdrawing_a_change_restores_the_default(world: World) -> None:
    require_review(world.engine)
    world.project(1, "public")
    world.run()
    unmap = propose(
        world.engine, framework="soc2", control_id="CC6.6", kind="unmap", rule_type=VISIBILITY
    ).json()
    analyst(world.engine, 2).post(f"{CHANGES}/{unmap['id']}/approve", json={})
    assert controls(world.engine)["soc2:CC6.6"]["status"] == "not_covered"

    withdraw = propose(
        world.engine,
        user_id=2,
        framework="soc2",
        control_id="CC6.6",
        kind="withdraw",
        supersedes_id=unmap["id"],
    ).json()
    pending = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.6",
        kind="withdraw",
        supersedes_id=unmap["id"],
    )
    approved = analyst(world.engine, 1).post(f"{CHANGES}/{withdraw['id']}/approve", json={})

    assert codes(pending) == ["supersede_pending"]
    assert approved.json()["state"] == "active"
    assert controls(world.engine)["soc2:CC6.6"]["status"] == "not_met"
    history = auditor(world.engine).get("/api/v1/controls/soc2/CC6.6").json()["changes"]
    assert [(c["kind"], c["state"]) for c in history] == [
        ("withdraw", "active"),
        ("unmap", "withdrawn"),
    ]
    again = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.6",
        kind="withdraw",
        supersedes_id=withdraw["id"],
    )
    assert codes(again) == ["supersedes_invalid_kind"]
    assert actions(world.engine)[-2:] == ["control_mapping.approved", "control_mapping.withdrawn"]


def test_a_control_with_its_rules_and_history(world: World) -> None:
    world.project(1, "public")
    world.project(2, "private")
    world.run()
    propose(
        world.engine,
        framework="nist_800_53_moderate",
        control_id="AC-6(5)",
        kind="map",
        rule_type=VISIBILITY,
    )

    response = auditor(world.engine).get("/api/v1/controls/nist_800_53_moderate/AC-6(5)")
    missing = auditor(world.engine).get("/api/v1/controls/soc2/CC9.9")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Privileged accounts are limited to designated people"
    assert body["rules"] == [
        {
            "policy": {"name": "default-gitlab-baseline", "version": 1},
            "rule_id": "project-visibility",
            "rule_type": VISIBILITY,
            "counts": {"pass": 1, "fail": 1, "unknown": 0, "excepted": 0, "projects": 2},
        }
    ]
    assert [(c["kind"], c["rule_type"]) for c in body["changes"]] == [("map", VISIBILITY)]
    assert missing.status_code == 404


def test_changes_are_listed_newest_first_with_filters(world: World) -> None:
    require_review(world.engine)
    first = propose(
        world.engine, framework="soc2", control_id="CC6.8", kind="map", rule_type=VISIBILITY
    ).json()
    second = propose(
        world.engine, framework="iso27001", control_id="A.8.9", kind="map", rule_type=BRANCH
    ).json()
    analyst(world.engine, 2).post(f"{CHANGES}/{first['id']}/reject", json={"note": "No."})

    def ids(query: str) -> list[int]:
        body = auditor(world.engine).get(f"{CHANGES}?{query}").json()
        return [item["id"] for item in body["items"]]

    page = auditor(world.engine).get(f"{CHANGES}?limit=1").json()
    assert page["review_required"] is True
    assert [i["id"] for i in page["items"]] == [second["id"]]
    assert page["next_before_id"] == second["id"]
    assert ids("state=proposed") == [second["id"]]
    assert ids("state=rejected") == [first["id"]]
    assert ids("framework=soc2") == [first["id"]]
    assert ids("framework=iso27001&control_id=A.8.9") == [second["id"]]


def test_the_trigger_keeps_changes_append_only(world: World) -> None:
    response = propose(
        world.engine, framework="soc2", control_id="CC6.8", kind="map", rule_type=VISIBILITY
    )
    change_id = response.json()["id"]

    def refused(statement: str) -> None:
        with world.engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(text(statement), {"id": change_id})

    refused("DELETE FROM control_mapping_change WHERE id = :id")
    refused("UPDATE control_mapping_change SET justification = 'x' || justification WHERE id = :id")
    refused("UPDATE control_mapping_change SET state = 'proposed' WHERE id = :id")
    refused(
        "UPDATE control_mapping_change SET state = 'withdrawn', decision_note = 'x' WHERE id = :id"
    )
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE control_mapping_change SET state = 'withdrawn' WHERE id = :id"),
            {"id": change_id},
        )
    refused("UPDATE control_mapping_change SET state = 'active' WHERE id = :id")


# Evidence export -----------------------------------------------------------------------------


def export(engine: Engine, scope: dict[str, Any]) -> Any:
    return client(engine, 7, Role.AUDITOR).post(
        "/api/v1/evidence/exports", json={"format": "json", "scope": scope}
    )


def test_the_manifest_records_the_mapping_in_force(world: World) -> None:
    world.project(1, "public")
    world.run()
    change = propose(
        world.engine,
        framework="soc2",
        control_id="CC6.8",
        kind="manual",
        review_by=in_days(60),
        evidence_ref="Endpoint protection console",
    ).json()

    response = export(world.engine, {"all": True})

    assert response.status_code == 200, response.text
    manifest = response.json()["manifest"]
    assert manifest["manifest_format"] == "baluarte.evidence-manifest/v6"
    mapping = manifest["control_mapping"]
    assert mapping["mapping_version"] == MAPPING_VERSION
    assert len(mapping["effective"]) == len(DEFAULT_MAPPING)
    assert mapping["changes"] == [
        {
            "id": change["id"],
            "framework": "soc2",
            "control_id": "CC6.8",
            "kind": "manual",
            "rule_type": None,
            "review_by": in_days(60),
            "self_approved": True,
            "mapping_version": MAPPING_VERSION,
            "justification_sha256": hashlib.sha256(WHY.encode()).hexdigest(),
        }
    ]


def test_an_export_scoped_to_a_control_holds_its_mapped_rules_only(world: World) -> None:
    world.project(1, "public")
    world.run()

    visibility = export(world.engine, {"framework": "soc2", "control_id": "CC6.6"}).json()
    branch = export(
        world.engine, {"framework": "nist_800_53_moderate", "control_id": "CM-5"}
    ).json()
    uncovered = export(world.engine, {"framework": "soc2", "control_id": "CC6.8"}).json()

    assert {e["rule_type"] for e in visibility["evaluations"]} == {VISIBILITY}
    assert [f["rule_id"] for f in visibility["findings"]] == ["project-visibility"]
    assert {e["rule_type"] for e in branch["evaluations"]} == {BRANCH}
    assert branch["findings"] == []
    assert uncovered["evaluations"] == []
    assert visibility["manifest"]["scope"] == {"framework": "soc2", "control_id": "CC6.6"}


def test_a_control_scope_must_name_a_known_control(world: World) -> None:
    assert export(world.engine, {"framework": "soc2"}).status_code == 422
    assert export(world.engine, {"control_id": "CC6.6"}).status_code == 422
    unknown = export(world.engine, {"framework": "soc2", "control_id": "CC9.9"})
    assert unknown.status_code == 422
    assert "CC9.9" in unknown.json()["detail"]
