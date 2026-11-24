# ruff: noqa: F811 (pytest fixtures imported from test_policy_assignments)
"""Configuration templates: versions, extends, assignments and effective settings (M14.1)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_policy_assignments import client_as, gitlab_env, instance_id  # noqa: F401

from baluarte.auth.roles import Role
from baluarte.db.models import AuditEvent, GitLabProject, TemplateAssignment
from baluarte.templates.loader import TemplateInvalid, parse_template

pytestmark = pytest.mark.db


def template(name: str, settings: str, extends: str | None = None) -> str:
    lines = [
        "apiVersion: baluarte.io/v1alpha1",
        "kind: ConfigurationTemplate",
        "metadata:",
        f"  name: {name}",
        "spec:",
    ]
    if extends:
        lines.append(f"  extends: {extends}")
    lines.append("  settings:")
    lines += [f"    {line}" for line in settings.strip().splitlines()]
    return "\n".join(lines) + "\n"


BASE = template(
    "base",
    """
visibility: private
default_branch_protected: true
merge_requests:
  min_approvals: 1
  forbid_author_approval: true
""",
)
SERVICE = template(
    "service",
    """
merge_requests:
  min_approvals: 2
security_features: [secret_scanning]
""",
    extends="base@1",
)
REASON = "The public SDK repository has to stay public for its users."


def save(client: TestClient, source: str) -> Any:
    return client.post("/api/v1/templates", json={"source_yaml": source})


def assign(client: TestClient, connection: int, name: str, version: int, **extra: Any) -> Any:
    body = {"connection_id": connection, "name": name, "version": version, **extra}
    return client.post("/api/v1/template-assignments", json=body)


def project_id(engine: Engine, gitlab_id: int) -> int:
    with Session(engine) as db:
        return db.scalars(
            select(GitLabProject.id).where(GitLabProject.gitlab_id == gitlab_id)
        ).one()


def actions(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return [e.action for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id))]


# --- the format ---


def test_the_format_names_exact_values_and_refuses_unknown_keys() -> None:
    parsed = parse_template(SERVICE)
    assert parsed.document.spec.extends == "base@1"
    assert parsed.document.spec.settings.merge_requests is not None

    for bad, path in [
        (template("x", "merge_method: ff"), "spec.settings.merge_method"),
        (template("x", "visibility: secret"), "spec.settings.visibility"),
        (
            template("x", "security_features: [push_protection]"),
            "spec.settings.security_features[0]",
        ),
        (template("x", "merge_requests: {}"), "spec.settings.merge_requests"),
        (template("x", "labels_consistent: false"), "spec.settings.labels_consistent"),
        (template("x", "visibility: private", extends="base"), "spec.extends"),
    ]:
        with pytest.raises(TemplateInvalid) as caught:
            parse_template(bad)
        assert path in [error.path for error in caught.value.errors], bad


# --- versions ---


def test_saving_adds_versions_and_identical_text_returns_the_latest(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    client = client_as(migrated_engine, Role.ANALYST)

    first = save(client, BASE)
    again = save(client, BASE)
    second = save(client, BASE.replace("min_approvals: 1", "min_approvals: 3"))

    assert (first.status_code, again.status_code, second.status_code) == (201, 200, 201)
    assert (first.json()["version"], again.json()["version"], second.json()["version"]) == (1, 1, 2)
    assert actions(migrated_engine) == ["template.version_created"] * 2
    listed = client.get("/api/v1/templates").json()["templates"]
    assert [(t["name"], [v["version"] for v in t["versions"]]) for t in listed] == [
        ("base", [2, 1])
    ]


def test_extends_resolves_a_saved_version(migrated_engine: Engine, instance_id: int) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    assert save(client, BASE).status_code == 201

    saved = save(client, SERVICE)

    assert saved.status_code == 201
    body = saved.json()
    assert body["extends"] == "base@1"
    assert body["chain"] == ["base@1"]
    assert body["settings"] == {
        "merge_requests.min_approvals": 2,
        "security_features": ["secret_scanning"],
    }
    assert body["resolved_settings"] == {
        "merge_requests.min_approvals": 2,
        "security_features": ["secret_scanning"],
        "visibility": "private",
        "default_branch_protected": True,
        "merge_requests.forbid_author_approval": True,
    }
    read = client.get("/api/v1/templates/service/versions/1")
    assert read.status_code == 200
    assert read.json()["source_yaml"] == SERVICE


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (template("service", "visibility: public", extends="base@9"), "no saved template"),
        (template("base", "visibility: public", extends="base@1"), "cannot extend itself"),
    ],
)
def test_extends_must_name_another_saved_version(
    migrated_engine: Engine,
    instance_id: int,
    source: str,
    message: str,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    save(client, BASE)

    response = save(client, source)

    assert response.status_code == 422
    [error] = response.json()["detail"]["errors"]
    assert error["path"] == "spec.extends"
    assert message in error["message"]


def test_chains_are_at_most_five_deep(migrated_engine: Engine, instance_id: int) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    assert save(client, template("t1", "visibility: private")).status_code == 201
    for depth in range(2, 6):
        source = template(f"t{depth}", "labels_consistent: true", extends=f"t{depth - 1}@1")
        assert save(client, source).status_code == 201

    too_deep = save(client, template("t6", "labels_consistent: true", extends="t5@1"))

    assert too_deep.status_code == 422
    assert "at most 5" in too_deep.json()["detail"]["errors"][0]["message"]


def test_settings_may_all_be_inherited_but_not_absent(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    save(client, BASE)
    empty = template("empty", "{}")

    assert save(client, empty).status_code == 422
    assert save(client, template("child", "{}", extends="base@1")).status_code == 201


def test_saved_versions_cannot_change(migrated_engine: Engine, instance_id: int) -> None:
    save(client_as(migrated_engine, Role.ADMIN), BASE)

    for statement in (
        "UPDATE configuration_template SET source_yaml = 'x'",
        "DELETE FROM configuration_template",
    ):
        with migrated_engine.connect() as connection, pytest.raises(DBAPIError):
            connection.execute(text(statement))


def test_auditors_read_but_cannot_save_or_assign(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    save(client_as(migrated_engine, Role.ADMIN), BASE)
    auditor = client_as(migrated_engine, Role.AUDITOR)

    assert auditor.get("/api/v1/templates").status_code == 200
    assert auditor.get("/api/v1/templates/settings").status_code == 200
    assert save(auditor, SERVICE).status_code == 403
    assert assign(auditor, instance_id, "base", 1, target_type="instance").status_code == 403


# --- assignments ---


def test_assigning_replaces_the_targets_template_and_audits(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    save(client, BASE)
    save(client, SERVICE)

    first = assign(client, instance_id, "base", 1, target_type="group", target_id=11)
    second = assign(client, instance_id, "service", 1, target_type="group", target_id=11)

    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json()["target"]["path"] == "platform/backend"
    with Session(migrated_engine) as db:
        [row] = db.scalars(select(TemplateAssignment)).all()
        assert row.id == second.json()["id"]
        event = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "template.assigned")
            .order_by(AuditEvent.id.desc())
        ).first()
        assert event is not None
        assert event.details["replaced"]["name"] == "base"

    removed = client.delete(f"/api/v1/template-assignments/{row.id}")
    assert removed.status_code == 204
    assert client.get("/api/v1/template-assignments").json()["assignments"] == []
    assert actions(migrated_engine)[-1] == "template.unassigned"


def test_assignments_need_an_active_target_and_a_saved_version(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    save(client, BASE)

    assert assign(client, instance_id, "base", 2, target_type="instance").status_code == 404
    assert (
        assign(client, instance_id, "base", 1, target_type="group", target_id=999).status_code
        == 404
    )
    assert (
        assign(client, instance_id, "base", 1, target_type="group", target_id=30).status_code == 409
    )
    assert assign(client, 999, "base", 1, target_type="instance").status_code == 404
    current = client.post(
        "/api/v1/template-assignments",
        json={"name": "base", "version": 1, "target_type": "instance"},
    )
    assert current.status_code == 201
    assert current.json()["connection_id"] == instance_id


def test_overrides_need_a_reason_and_valid_settings(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    save(client, BASE)
    target = {"target_type": "project", "target_id": 101}

    no_reason = assign(client, instance_id, "base", 1, **target, overrides={"visibility": "public"})
    short = assign(
        client,
        instance_id,
        "base",
        1,
        **target,
        overrides={"visibility": "public"},
        override_reason="because",
    )
    unknown = assign(
        client,
        instance_id,
        "base",
        1,
        **target,
        overrides={"topics": ["x"]},
        override_reason=REASON,
    )
    reason_only = assign(client, instance_id, "base", 1, **target, override_reason=REASON)

    for response in (no_reason, short, unknown, reason_only):
        assert response.status_code == 422, response.json()
    assert unknown.json()["detail"]["errors"][0]["path"] == "overrides.topics"
    ok = assign(
        client,
        instance_id,
        "base",
        1,
        **target,
        overrides={"visibility": "public"},
        override_reason=REASON,
    )
    assert ok.status_code == 201
    assert ok.json()["overrides"] == {"visibility": "public"}


# --- effective settings ---


def test_the_nearest_assignment_decides_each_setting(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    save(client, BASE)
    save(client, SERVICE)
    assign(client, instance_id, "base", 1, target_type="instance")
    assign(client, instance_id, "service", 1, target_type="group", target_id=11)
    assign(
        client,
        instance_id,
        "service",
        1,
        target_type="project",
        target_id=100,
        overrides={"visibility": "public"},
        override_reason=REASON,
    )

    effective = client.get(f"/api/v1/templates/effective/{project_id(migrated_engine, 100)}")

    assert effective.status_code == 200
    body = effective.json()
    assert body["provider"] == "gitlab"
    assert body["ancestry"] == "complete"
    assert [a["assigned_to"]["target_type"] for a in body["assignments"]] == [
        "project",
        "group",
        "instance",
    ]
    settings = {s["key"]: s for s in body["settings"]}
    assert list(settings) == [
        "visibility",
        "default_branch_protected",
        "merge_requests.min_approvals",
        "merge_requests.forbid_author_approval",
        "security_features",
    ]

    visibility = settings["visibility"]
    assert visibility["value"] == "public"
    assert visibility["decided_by"]["origin"] == "override"
    assert visibility["decided_by"]["assigned_to"]["target_id"] == 100
    # The project's template inherits private from base, then the group's, then the connection's.
    assert [(r["origin"], r["assigned_to"]["target_type"]) for r in visibility["replaced"]] == [
        ("extends", "project"),
        ("extends", "group"),
        ("template", "instance"),
    ]
    approvals = settings["merge_requests.min_approvals"]
    assert approvals["value"] == 2
    assert (approvals["decided_by"]["template"], approvals["decided_by"]["origin"]) == (
        "service",
        "template",
    )
    assert approvals["support"] == "plan"
    assert settings["security_features"]["support"] == "unsupported"
    assert settings["default_branch_protected"]["decided_by"]["origin"] == "extends"


def test_a_personal_project_takes_only_the_connections_template(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    save(client, BASE)
    save(client, SERVICE)
    assign(client, instance_id, "base", 1, target_type="instance")
    assign(client, instance_id, "service", 1, target_type="group", target_id=10)

    body = client.get(f"/api/v1/templates/effective/{project_id(migrated_engine, 102)}").json()

    assert [a["template"] for a in body["assignments"]] == ["base"]
    assert {s["key"]: s["value"] for s in body["settings"]}["merge_requests.min_approvals"] == 1


def test_a_gap_in_the_ancestry_is_reported(migrated_engine: Engine, instance_id: int) -> None:
    client = client_as(migrated_engine, Role.ADMIN)
    body = client.get(f"/api/v1/templates/effective/{project_id(migrated_engine, 103)}").json()

    assert body["ancestry"] == "incomplete"
    assert body["settings"] == []
    assert client.get("/api/v1/templates/effective/999999").status_code == 404


def test_the_support_matrix_covers_every_setting(
    migrated_engine: Engine,
    instance_id: int,
) -> None:
    rows = client_as(migrated_engine, Role.AUDITOR).get("/api/v1/templates/settings").json()
    by_key = {row["key"]: row for row in rows}

    assert by_key["merge_requests.forbid_override"]["github"] == "unsupported"
    assert by_key["security_features"]["gitlab"] == "unsupported"
    assert by_key["labels_consistent"] == {
        "key": "labels_consistent",
        "gitlab": "supported",
        "gitlab_note": None,
        "github": "supported",
        "github_note": None,
    }
