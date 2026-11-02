"""GitLab security policy discovery: GraphQL over GET, evidence, catalogue, job and read API."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL, FakeGitLab, graphql, policy_node, settings
from oidc_fake import make_settings
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.dependencies import Principal, auth_settings, current_principal, database_engine
from baluarte.auth.roles import Role
from baluarte.db.models import (
    SECURITY_POLICY_TYPES,
    AuditEvent,
    CollectionJob,
    CollectionRun,
    GitLabGroup,
    GitLabProject,
    GitLabSecurityPolicy,
    Observation,
)
from baluarte.evidence.observations import verify
from baluarte.integrations.gitlab import security_policies as sp
from baluarte.integrations.gitlab.capabilities import detect, record_snapshot
from baluarte.integrations.gitlab.client import (
    GitLabClient,
    GraphQLError,
    GraphQLForbidden,
    InvalidResponse,
    UnsupportedField,
)
from baluarte.integrations.gitlab.freshness import report
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import (
    DISCOVER_SECURITY_POLICIES,
    make_discover_security_policies,
    queue_security_policies,
)
from baluarte.jobs import queue
from baluarte.jobs.worker import Worker
from baluarte.main import create_app
from baluarte.policy.evaluation.runner import EVALUATE

CORPORATE_POLICIES = "corporate/security-policy-project"
ACME_POLICIES = "acme/platform/security-policies"

GROUPS = [
    (1, "corporate", None),
    (2, "finance", None),
    (3, "facilities", None),
    (4, "acme", None),
    (5, "acme/platform", 4),
]
PROJECTS = [
    (10, "finance/ledger", 2),
    (11, "acme/portal", 4),
    (12, CORPORATE_POLICIES, 1),
]
LINKS = {
    "finance": CORPORATE_POLICIES,
    "facilities": CORPORATE_POLICIES,
    "acme": ACME_POLICIES,
}


def corporate(source: str, inherited: bool = False) -> dict[str, list[dict[str, Any]]]:
    return {
        "scan_execution": [policy_node("Scan policy", source, inherited=inherited)],
        "approval": [policy_node("Peer approval", source, inherited=inherited)],
    }


def licence(inherited: bool) -> dict[str, list[dict[str, Any]]]:
    return {"approval": [policy_node("License Approval Policy", "acme", inherited=inherited)]}


POLICIES: dict[str, dict[str, list[dict[str, Any]]]] = {
    "finance": corporate("finance"),
    "facilities": corporate("facilities"),
    "acme": licence(False),
    "acme/platform": licence(True),
    "finance/ledger": corporate("finance", inherited=True),
    "acme/portal": licence(True),
}


def fake_gitlab(**options: Any) -> FakeGitLab:
    fake = FakeGitLab()
    fake.graphql(graphql(POLICIES, LINKS, **options))
    return fake


def client(fake: FakeGitLab) -> GitLabClient:
    return GitLabClient(settings(), transport=fake.transport, sleep=lambda _: None)


def seed(engine: Engine, fake: FakeGitLab | None = None) -> int:
    """The instance with a capability snapshot from ``fake``, and the inventory above."""
    fake = fake or fake_gitlab()
    snapshot = detect(client(fake))
    with Session(engine) as db:
        instance = record_snapshot(db, URL, snapshot)
        db.flush()
        for gitlab_id, path, parent in GROUPS:
            db.add(
                GitLabGroup(
                    instance_id=instance.id,
                    gitlab_id=gitlab_id,
                    parent_gitlab_id=parent,
                    full_path=path,
                    name=path.rsplit("/", 1)[-1],
                    visibility="private",
                )
            )
        for gitlab_id, path, namespace in PROJECTS:
            db.add(
                GitLabProject(
                    instance_id=instance.id,
                    gitlab_id=gitlab_id,
                    namespace_gitlab_id=namespace,
                    namespace_kind="group",
                    path_with_namespace=path,
                    name=path.rsplit("/", 1)[-1],
                    visibility="private",
                    default_branch="main",
                )
            )
        db.commit()
        return instance.id


def availability(engine: Engine) -> dict[str, sp.TypeStatus]:
    with Session(engine) as db:
        return sp.type_availability(instance_for(db, URL).capability_snapshot)


def catalogue(engine: Engine) -> dict[tuple[str, str, str], GitLabSecurityPolicy]:
    with Session(engine) as db:
        return {
            (row.policy_type, row.origin_path, row.name): row
            for row in db.scalars(select(GitLabSecurityPolicy))
        }


def observations(engine: Engine) -> list[Observation]:
    with Session(engine) as db:
        return list(
            db.scalars(
                select(Observation)
                .where(Observation.source.in_((sp.GROUP_SOURCE, sp.PROJECT_SOURCE)))
                .order_by(Observation.id)
            )
        )


# GraphQL over GET


def test_graphql_sends_a_get_with_query_and_variables() -> None:
    fake = fake_gitlab()

    data = client(fake).graphql(sp.link_probe_query(), {"path": "finance"})

    [request] = fake.requests
    assert request.method == "GET"
    assert request.url.host == "gitlab.example.test"
    assert request.url.path == "/api/graphql"
    assert request.url.params["query"] == sp.link_probe_query()
    assert json.loads(request.url.params["variables"]) == {"path": "finance"}
    assert request.headers["PRIVATE-TOKEN"] == TOKEN
    assert data == {"group": {"securityPolicyProject": {"fullPath": CORPORATE_POLICIES}}}


def test_graphql_on_a_relative_url_install_stays_on_the_instance() -> None:
    fake = FakeGitLab({"/gitlab/api/graphql": [graphql()]})

    with GitLabClient(
        settings("https://gitlab.example.test/gitlab"), transport=fake.transport
    ) as c:
        c.graphql(sp.link_probe_query(), {"path": "corporate"})

    assert [str(r.url).split("?")[0] for r in fake.requests] == [
        "https://gitlab.example.test/gitlab/api/graphql"
    ]


@pytest.mark.parametrize(
    "query", ["mutation { securityPolicyProjectAssign { errors } }", "subscription { x }"]
)
def test_graphql_refuses_anything_but_a_query(query: str) -> None:
    fake = fake_gitlab()

    with pytest.raises(ValueError, match="only GraphQL queries"):
        client(fake).graphql(query)
    assert fake.requests == []


def test_graphql_request_size_is_limited() -> None:
    fake = fake_gitlab()

    with pytest.raises(ValueError, match="exceeds"):
        client(fake).graphql("{ " + "x " * 4000 + "}")
    assert fake.requests == []


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("Field 'approvalPolicies' doesn't exist on type 'Group'", UnsupportedField),
        ("you don't have permission to perform this action", GraphQLForbidden),
        ("Query has complexity of 347, which exceeds max complexity of 300", GraphQLError),
    ],
)
def test_graphql_errors_are_classified(message: str, error: type[Exception]) -> None:
    fake = FakeGitLab()
    fake.graphql(
        httpx.Response(200, json={"data": {"group": None}, "errors": [{"message": message}]})
    )

    with pytest.raises(error, match="GraphQL: "):
        client(fake).graphql(sp.link_probe_query(), {"path": "corporate"})


def test_graphql_error_messages_are_cut_short() -> None:
    fake = FakeGitLab()
    fake.graphql(httpx.Response(200, json={"errors": [{"message": "x" * 5000}]}))

    with pytest.raises(GraphQLError) as caught:
        client(fake).graphql(sp.link_probe_query(), {"path": "corporate"})
    assert len(str(caught.value)) < 220


def test_graphql_without_data_is_an_invalid_response() -> None:
    fake = FakeGitLab()
    fake.graphql(httpx.Response(200, json={"data": None}))

    with pytest.raises(InvalidResponse, match="no data"):
        client(fake).graphql(sp.link_probe_query(), {"path": "corporate"})


def test_graphql_redirects_are_not_followed() -> None:
    fake = FakeGitLab()
    fake.graphql(httpx.Response(302, headers={"Location": "https://evil.example/graphql"}))

    with pytest.raises(InvalidResponse, match="redirected"):
        client(fake).graphql(sp.link_probe_query(), {"path": "corporate"})
    assert [r.url.host for r in fake.requests] == ["gitlab.example.test"]


# Capability probes


def probe_statuses(fake: FakeGitLab) -> dict[str, str]:
    snapshot = detect(client(fake))
    return {
        name: probe.status
        for name, probe in snapshot.probes.items()
        if name.startswith("security_policies.")
    }


def test_probes_find_each_policy_type() -> None:
    assert probe_statuses(fake_gitlab()) == {
        "security_policies.read": "supported",
        **{f"security_policies.{t}": "supported" for t in SECURITY_POLICY_TYPES},
    }


def test_a_field_this_gitlab_lacks_is_unsupported_not_absent() -> None:
    statuses = probe_statuses(fake_gitlab(unsupported=["pipelineExecutionPolicies"]))

    assert statuses["security_policies.pipeline_execution"] == "unsupported"
    assert statuses["security_policies.scan_execution"] == "supported"


def test_a_refused_tier_is_forbidden_and_unknown_not_unlicensed() -> None:
    fake = fake_gitlab(forbidden=["platform"])
    snapshot = detect(client(fake))

    assert {
        name: probe.status
        for name, probe in snapshot.probes.items()
        if name.startswith("security_policies.")
    } == dict.fromkeys(
        ["security_policies.read"] + [f"security_policies.{t}" for t in SECURITY_POLICY_TYPES],
        "forbidden",
    )
    assert snapshot.tier is None
    assert sp.type_availability(snapshot.model_dump(mode="json")) == dict.fromkeys(
        SECURITY_POLICY_TYPES, "unknown"
    )
    assert not sp.readable(snapshot.model_dump(mode="json"))


def test_no_visible_group_skips_the_probes() -> None:
    fake = fake_gitlab()
    fake.json("/groups", body=[])

    assert set(probe_statuses(fake).values()) == {"skipped"}


def test_security_policy_probes_do_not_change_the_core_status() -> None:
    snapshot = detect(client(fake_gitlab(forbidden=["platform"])))

    assert snapshot.status == "ok"


def test_availability_and_readability_from_a_snapshot() -> None:
    snapshot = detect(client(fake_gitlab(unsupported=["vulnerabilityManagementPolicies"])))
    dumped = snapshot.model_dump(mode="json")

    assert sp.type_availability(dumped) == {
        "scan_execution": "ok",
        "approval": "ok",
        "pipeline_execution": "ok",
        "vulnerability_management": "unsupported",
    }
    assert sp.readable(dumped)
    assert sp.type_availability(None) == dict.fromkeys(SECURITY_POLICY_TYPES, "unknown")
    assert not sp.readable(None)
    assert not sp.readable({"status": "failed", "probes": {}})


# Reading one target


ALL_OK: dict[str, sp.TypeStatus] = dict.fromkeys(SECURITY_POLICY_TYPES, "ok")


def test_a_project_asks_gitlab_for_the_policies_whose_scope_includes_it() -> None:
    fake = fake_gitlab()

    observed = sp.read_target(client(fake), sp.Target("project", 7, "finance/ledger"), ALL_OK)

    queries = [request.url.params["query"] for request in fake.requests]
    # Two queries of two types each keep each under GitLab's complexity limit.
    assert len(queries) == 2
    assert all("project(fullPath: $path)" in q for q in queries)
    assert all(q.count("includeUnscoped: false") == 2 for q in queries)
    assert all(q.count("relationship: INHERITED") == 2 for q in queries)
    assert observed.collection_status == "ok"
    assert observed.source == sp.PROJECT_SOURCE
    assert observed.source_ref == "finance/ledger"
    assert observed.payload is not None
    [scan] = observed.payload["types"]["scan_execution"]["policies"]
    assert scan == {
        "name": "Scan policy",
        "description": "Scan policy description",
        "enabled": True,
        "yaml": "- name: Scan policy\n  enabled: true\n",
        "updated_at": "2026-10-23T14:06:13Z",
        "source": {"type": "group", "path": "finance", "inherited": True},
        "scope": {
            "compliance_frameworks": [],
            "including_projects": [],
            "excluding_projects": [],
            "including_groups": [],
            "excluding_groups": [],
            "excluding_archived_projects": False,
            "excluding_personal_projects": False,
            "truncated": False,
        },
    }
    assert observed.payload["types"]["pipeline_execution"] == {
        "status": "ok",
        "truncated": False,
        "policies": [],
    }


def test_a_group_query_reads_its_link_and_inherited_policies() -> None:
    fake = fake_gitlab()

    observed = sp.read_target(client(fake), sp.Target("group", 1, "finance"), ALL_OK)

    assert all("includeUnscoped" not in r.url.params["query"] for r in fake.requests)
    assert observed.payload is not None
    assert observed.payload["security_policy_project"] == CORPORATE_POLICIES


def test_unavailable_types_are_recorded_and_never_queried() -> None:
    fake = fake_gitlab()
    statuses: dict[str, sp.TypeStatus] = {
        "scan_execution": "ok",
        "approval": "ok",
        "pipeline_execution": "unsupported",
        "vulnerability_management": "unknown",
    }

    observed = sp.read_target(client(fake), sp.Target("group", 1, "finance"), statuses)

    assert len(fake.requests) == 1
    query = fake.requests[0].url.params["query"]
    assert "pipelineExecutionPolicies" not in query
    assert "vulnerabilityManagementPolicies" not in query
    assert observed.collection_status == "partial"
    assert observed.payload is not None
    assert observed.payload["types"]["pipeline_execution"]["status"] == "unsupported"
    assert observed.payload["types"]["vulnerability_management"]["status"] == "unknown"


def test_a_listing_with_more_pages_is_partial() -> None:
    observed = sp.read_target(
        client(fake_gitlab(truncated=["finance"])), sp.Target("group", 1, "finance"), ALL_OK
    )

    assert observed.collection_status == "partial"
    assert observed.payload is not None
    assert observed.payload["types"]["scan_execution"]["truncated"] is True


@pytest.mark.parametrize(
    ("options", "status"),
    [
        ({"hidden": ["finance"]}, "not_found"),
        ({"forbidden": ["finance"]}, "forbidden"),
        ({"unsupported": ["scanExecutionPolicies"]}, "unsupported"),
    ],
)
def test_an_unreadable_target_has_no_payload(options: dict[str, Any], status: str) -> None:
    observed = sp.read_target(
        client(fake_gitlab(**options)), sp.Target("group", 1, "finance"), ALL_OK
    )

    assert (observed.collection_status, observed.payload) == (status, None)
    assert observed.error


def test_a_rejected_token_stops_reading() -> None:
    fake = fake_gitlab()
    fake.graphql(httpx.Response(401))

    with pytest.raises(sp.TokenRejected):
        sp.read_target(client(fake), sp.Target("group", 1, "finance"), ALL_OK)


# Collection, evidence and catalogue


@pytest.mark.db
def test_collection_records_evidence_and_one_catalogue_row_per_policy(
    migrated_engine: Engine,
) -> None:
    instance_id = seed(migrated_engine)
    fake = fake_gitlab()

    result = sp.collect(client(fake), migrated_engine, instance_id, ALL_OK)

    assert (result.groups.as_counts(), result.projects.as_counts()) == (
        {"read": 5, "unread": 0},
        {"read": 3, "unread": 0},
    )
    assert result.incomplete == 0
    assert result.policies == {"active": 3, "new": 3, "missing": 0}
    assert result.by_type == {"scan_execution": 1, "approval": 2}
    assert {r.method for r in fake.requests} == {"GET"}
    rows = observations(migrated_engine)
    assert len(rows) == 8
    assert all(verify(row) and row.collection_status == "ok" for row in rows)
    found = catalogue(migrated_engine)
    assert set(found) == {
        ("scan_execution", CORPORATE_POLICIES, "Scan policy"),
        ("approval", CORPORATE_POLICIES, "Peer approval"),
        ("approval", ACME_POLICIES, "License Approval Policy"),
    }
    scan = found[("scan_execution", CORPORATE_POLICIES, "Scan policy")]
    assert scan.origin_kind == "policy_project"
    assert scan.linked_from == [
        {"type": "group", "path": "facilities"},
        {"type": "group", "path": "finance"},
    ]
    assert scan.yaml == "- name: Scan policy\n  enabled: true\n"
    assert scan.yaml_digest is not None and len(scan.yaml_digest) == 64
    assert scan.enabled is True
    assert scan.gitlab_updated_at == datetime(2026, 10, 23, 14, 6, 13, tzinfo=UTC)


@pytest.mark.db
def test_unchanged_policies_are_not_recorded_again(migrated_engine: Engine) -> None:
    instance_id = seed(migrated_engine)
    sp.collect(client(fake_gitlab()), migrated_engine, instance_id, ALL_OK)

    second = sp.collect(client(fake_gitlab()), migrated_engine, instance_id, ALL_OK)

    assert len(observations(migrated_engine)) == 8
    assert second.policies == {"active": 3, "new": 0, "missing": 0}


@pytest.mark.db
def test_a_policy_source_baluarte_cannot_see_is_keyed_by_its_link(
    migrated_engine: Engine,
) -> None:
    instance_id = seed(migrated_engine)
    fake = FakeGitLab()
    fake.graphql(
        graphql({"finance/ledger": {"approval": [policy_node("Hidden", "secret-parent")]}})
    )

    sp.collect(client(fake), migrated_engine, instance_id, ALL_OK)

    [row] = catalogue(migrated_engine).values()
    assert (row.origin_kind, row.origin_path) == ("linked_source", "secret-parent")


@pytest.mark.db
def test_policies_are_marked_missing_only_after_a_complete_run(migrated_engine: Engine) -> None:
    instance_id = seed(migrated_engine)
    sp.collect(client(fake_gitlab()), migrated_engine, instance_id, ALL_OK)
    gone = {path: types for path, types in POLICIES.items() if not path.startswith("acme")}

    # One target unread: nothing can be confirmed gone.
    incomplete = FakeGitLab()
    incomplete.graphql(graphql(gone, LINKS, forbidden=["corporate"]))
    sp.collect(client(incomplete), migrated_engine, instance_id, ALL_OK)
    licence_row = ("approval", ACME_POLICIES, "License Approval Policy")
    assert catalogue(migrated_engine)[licence_row].state == "active"

    complete = FakeGitLab()
    complete.graphql(graphql(gone, LINKS))
    result = sp.collect(client(complete), migrated_engine, instance_id, ALL_OK)

    assert result.policies["missing"] == 1
    assert catalogue(migrated_engine)[licence_row].state == "missing"


@pytest.mark.db
def test_a_rejected_token_records_the_rest_as_not_read(migrated_engine: Engine) -> None:
    instance_id = seed(migrated_engine)
    fake = FakeGitLab()
    fake.graphql(httpx.Response(401))

    result = sp.collect(client(fake), migrated_engine, instance_id, ALL_OK)

    assert len(fake.requests) == 1
    assert result.unread == 8
    rows = observations(migrated_engine)
    assert [row.payload for row in rows] == [None] * 8
    assert "rejected the token" in (rows[-1].error or "")


# The job


@pytest.fixture
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


def run_discovery(engine: Engine, fake: FakeGitLab) -> CollectionJob:
    with Session(engine) as db:
        job_id = queue.enqueue(db, DISCOVER_SECURITY_POLICIES, "test-security-policies")
        db.commit()
    handler = make_discover_security_policies(fake.transport)
    assert Worker(engine, {DISCOVER_SECURITY_POLICIES: handler}).run_once()
    with Session(engine) as db:
        return db.get_one(CollectionJob, job_id)


def last_run(engine: Engine) -> CollectionRun:
    with Session(engine) as db:
        return db.scalars(
            select(CollectionRun)
            .where(CollectionRun.kind == "security_policies")
            .order_by(CollectionRun.id.desc())
        ).first() or pytest.fail("no security policy run")


@pytest.mark.db
def test_the_job_records_a_run_with_counts(migrated_engine: Engine, gitlab_env: None) -> None:
    seed(migrated_engine)

    job = run_discovery(migrated_engine, fake_gitlab())

    assert job.state == "succeeded"
    run = last_run(migrated_engine)
    assert (run.status, run.seen, run.error) == ("succeeded", 8, None)
    assert run.counts == {
        "groups": {"read": 5, "unread": 0},
        "projects": {"read": 3, "unread": 0},
        "policies": {"active": 3, "new": 3, "missing": 0},
        "by_type": {"approval": 2, "scan_execution": 1},
        "types_unavailable": [],
    }
    with Session(migrated_engine) as db:
        run_ids = set(
            db.scalars(
                select(Observation.collection_run_id).where(
                    Observation.source.in_((sp.GROUP_SOURCE, sp.PROJECT_SOURCE))
                )
            )
        )
    assert run_ids == {run.id}


@pytest.mark.db
def test_an_unsupported_type_makes_the_run_partial(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = fake_gitlab(unsupported=["vulnerabilityManagementPolicies"])
    seed(migrated_engine, fake)

    run_discovery(migrated_engine, fake)

    run = last_run(migrated_engine)
    assert run.status == "partial"
    assert run.counts["types_unavailable"] == ["vulnerability_management"]
    assert "unknown, unsupported or cut short" in (run.error or "")


@pytest.mark.db
def test_the_job_fails_when_policies_are_not_readable(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = fake_gitlab(forbidden=["platform"])
    seed(migrated_engine, fake)
    probed = len(fake.requests)

    job = run_discovery(migrated_engine, fake)

    assert job.state == "failed"
    run = last_run(migrated_engine)
    assert run.status == "failed"
    assert "not readable" in (run.error or "")
    assert len(fake.requests) == probed


def evaluations_queued(engine: Engine) -> list[str]:
    with Session(engine) as db:
        return list(
            db.scalars(select(CollectionJob.idempotency_key).where(CollectionJob.kind == EVALUATE))
        )


@pytest.mark.db
def test_policy_discovery_queues_one_evaluation_when_it_finishes(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = fake_gitlab(unsupported=["vulnerabilityManagementPolicies"])
    seed(migrated_engine, fake)

    run_discovery(migrated_engine, fake)

    # A partial run is evaluated too: the rules decide which types they can judge.
    assert evaluations_queued(migrated_engine) == [
        f"{EVALUATE}:after-run:{last_run(migrated_engine).id}"
    ]


@pytest.mark.db
def test_failed_policy_discovery_queues_no_evaluation(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = fake_gitlab(forbidden=["platform"])
    seed(migrated_engine, fake)

    run_discovery(migrated_engine, fake)

    assert evaluations_queued(migrated_engine) == []


@pytest.mark.db
def test_project_discovery_queues_policy_discovery_only_when_readable(
    migrated_engine: Engine,
) -> None:
    seed(migrated_engine)
    with Session(migrated_engine) as db:
        assert queue_security_policies(db, URL, 41) is not None
        # Once per project discovery run.
        assert queue_security_policies(db, URL, 41) == queue_security_policies(db, URL, 41)
        db.commit()

    with Session(migrated_engine) as db:
        record_snapshot(db, URL, detect(client(fake_gitlab(forbidden=["platform"]))))
        assert queue_security_policies(db, URL, 42) is None
        assert queue_security_policies(db, "https://other.example.test", 43) is None


@pytest.mark.db
def test_policy_discovery_does_not_change_overall_freshness(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = fake_gitlab(forbidden=["platform"])
    instance_id = seed(migrated_engine, fake)
    run_discovery(migrated_engine, fake)

    with Session(migrated_engine) as db:
        freshness = report(db, instance_id, settings())

    assert set(freshness.collections) == {"capabilities", "groups", "projects"}


# Read API


def client_as(engine: Engine, fake: FakeGitLab, *roles: Role) -> TestClient:
    app = create_app()
    app.dependency_overrides[database_engine] = lambda: engine
    app.dependency_overrides[current_principal] = lambda: Principal(
        user_id=1,
        subject="user-1",
        email=None,
        display_name=None,
        roles=frozenset(roles),
        session_id=1,
        session_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        csrf_token="csrf",
    )
    app.dependency_overrides[auth_settings] = lambda: make_settings()
    app.dependency_overrides[gitlab_transport] = lambda: fake.transport
    return TestClient(app)


def discovered(engine: Engine) -> FakeGitLab:
    fake = fake_gitlab()
    run_discovery(engine, fake)
    return fake


@pytest.mark.db
@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_every_role_lists_policies(migrated_engine: Engine, gitlab_env: None, role: Role) -> None:
    seed(migrated_engine)
    fake = discovered(migrated_engine)
    requests_before = len(fake.requests)

    response = client_as(migrated_engine, fake, role).get("/api/v1/gitlab/security-policies")

    assert response.status_code == 200
    assert len(fake.requests) == requests_before  # reads never call GitLab
    body = response.json()
    assert body["readable"] is True
    assert body["availability"] == dict.fromkeys(SECURITY_POLICY_TYPES, "ok")
    assert body["collection"]["status"] == "fresh"
    assert body["next_after_id"] is None
    by_name = {item["name"]: item for item in body["items"]}
    assert set(by_name) == {"Scan policy", "Peer approval", "License Approval Policy"}
    scan = by_name["Scan policy"]
    assert scan["origin_path"] == CORPORATE_POLICIES
    assert scan["reaches"] == {"groups": 2, "projects": 1}
    assert scan["linked_from"] == [
        {"type": "group", "path": "facilities"},
        {"type": "group", "path": "finance"},
    ]
    assert "yaml" not in scan
    assert by_name["License Approval Policy"]["reaches"] == {"groups": 2, "projects": 1}


@pytest.mark.db
def test_list_filters(migrated_engine: Engine, gitlab_env: None) -> None:
    seed(migrated_engine)
    api = client_as(migrated_engine, discovered(migrated_engine), Role.AUDITOR)

    def names(**params: Any) -> list[str]:
        response = api.get("/api/v1/gitlab/security-policies", params=params)
        return sorted(item["name"] for item in response.json()["items"])

    assert names(policy_type="scan_execution") == ["Scan policy"]
    assert names(search="licen") == ["License Approval Policy"]
    assert names(state="missing") == []
    assert len(names(limit=1)) == 1


@pytest.mark.db
def test_detail_has_yaml_scope_and_where_it_reaches(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    seed(migrated_engine)
    api = client_as(migrated_engine, discovered(migrated_engine), Role.AUDITOR)
    [scan] = [
        item
        for item in api.get("/api/v1/gitlab/security-policies").json()["items"]
        if item["name"] == "Scan policy"
    ]

    body = api.get(f"/api/v1/gitlab/security-policies/{scan['id']}").json()

    assert body["yaml"] == "- name: Scan policy\n  enabled: true\n"
    assert body["scope"]["excluding_projects"] == []
    assert [(t["path"], t["via"], t["inherited"]) for t in body["reached"]["groups"]] == [
        ("facilities", "facilities", False),
        ("finance", "finance", False),
    ]
    assert [(t["path"], t["via"], t["inherited"]) for t in body["reached"]["projects"]] == [
        ("finance/ledger", "finance", True)
    ]
    assert api.get("/api/v1/gitlab/security-policies/999999").status_code == 404


@pytest.mark.db
def test_every_read_carries_the_discovery_context(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    # The web UI reads readable, availability and collection from each of these responses.
    seed(migrated_engine)
    api = client_as(migrated_engine, discovered(migrated_engine), Role.AUDITOR)
    listing = api.get("/api/v1/gitlab/security-policies").json()
    first = listing["items"][0]["id"]
    for body in (
        listing,
        api.get(f"/api/v1/gitlab/security-policies/{first}").json(),
        api.get("/api/v1/gitlab/security-policies/coverage").json(),
    ):
        assert body["readable"] is True
        assert set(body["availability"]) == {
            "scan_execution",
            "approval",
            "pipeline_execution",
            "vulnerability_management",
        }
        assert "status" in body["collection"]


@pytest.mark.db
def test_coverage_marks_gaps_only_when_everything_was_read(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    seed(migrated_engine)
    api = client_as(migrated_engine, discovered(migrated_engine), Role.ANALYST)

    body = api.get("/api/v1/gitlab/security-policies/coverage").json()

    groups = {row["path"]: row for row in body["groups"]}
    projects = {row["path"]: row for row in body["projects"]}
    assert {path: row["status"] for path, row in groups.items()} == {
        "corporate": "not_covered",
        "facilities": "covered",
        "finance": "covered",
        "acme": "covered",
        "acme/platform": "covered",
    }
    assert {path: row["status"] for path, row in projects.items()} == {
        CORPORATE_POLICIES: "not_covered",
        "finance/ledger": "covered",
        "acme/portal": "covered",
    }
    assert body["summary"] == {
        "groups": {"covered": 4, "not_covered": 1, "unknown": 0},
        "projects": {"covered": 2, "not_covered": 1, "unknown": 0},
    }
    finance = groups["finance"]
    assert finance["security_policy_project"] == CORPORATE_POLICIES
    assert finance["types"]["scan_execution"]["enabled"] == 1
    [ref] = finance["types"]["scan_execution"]["policies"]
    assert ref["name"] == "Scan policy" and ref["id"] is not None
    assert finance["observation"]["collection_status"] == "ok"
    gaps = api.get(
        "/api/v1/gitlab/security-policies/coverage", params={"status": "not_covered"}
    ).json()
    assert [row["path"] for row in gaps["groups"]] == ["corporate"]
    only_groups = api.get(
        "/api/v1/gitlab/security-policies/coverage", params={"kind": "group"}
    ).json()
    assert only_groups["projects"] == []


@pytest.mark.db
def test_coverage_is_unknown_when_not_read_or_a_type_was_unavailable(
    migrated_engine: Engine, gitlab_env: None
) -> None:
    fake = fake_gitlab(unsupported=["vulnerabilityManagementPolicies"])
    seed(migrated_engine, fake)
    api = client_as(migrated_engine, fake, Role.ANALYST)

    never = api.get("/api/v1/gitlab/security-policies/coverage").json()
    assert {row["status"] for row in never["groups"]} == {"unknown"}
    assert never["groups"][0]["reason"] == "security policies have not been read for this target"
    assert never["collection"]["status"] == "never"

    run_discovery(migrated_engine, fake)
    body = api.get("/api/v1/gitlab/security-policies/coverage").json()
    corporate_row = next(row for row in body["groups"] if row["path"] == "corporate")
    assert corporate_row["status"] == "unknown"
    assert "vulnerability_management" in corporate_row["reason"]
    assert corporate_row["types"]["vulnerability_management"]["status"] == "unsupported"
    finance_row = next(row for row in body["groups"] if row["path"] == "finance")
    assert finance_row["status"] == "covered"


@pytest.mark.db
def test_without_gitlab_the_views_are_empty(migrated_engine: Engine) -> None:
    api = client_as(migrated_engine, FakeGitLab(), Role.AUDITOR)

    listing = api.get("/api/v1/gitlab/security-policies").json()
    coverage = api.get("/api/v1/gitlab/security-policies/coverage").json()

    assert (listing["items"], listing["readable"], listing["collection"]["status"]) == (
        [],
        False,
        "never",
    )
    assert (coverage["groups"], coverage["projects"]) == ([], [])


@pytest.mark.db
@pytest.mark.parametrize("role", [Role.ANALYST, Role.AUDITOR])
def test_only_admins_queue_discovery(migrated_engine: Engine, gitlab_env: None, role: Role) -> None:
    response = client_as(migrated_engine, FakeGitLab(), role).post(
        "/api/v1/gitlab/security-policies/discover"
    )

    assert response.status_code == 403


@pytest.mark.db
def test_queue_discovery_is_audited(migrated_engine: Engine, gitlab_env: None) -> None:
    response = client_as(migrated_engine, FakeGitLab(), Role.ADMIN).post(
        "/api/v1/gitlab/security-policies/discover", headers={"X-CSRF-Token": "csrf"}
    )

    assert response.status_code == 202
    with Session(migrated_engine) as db:
        job = db.get_one(CollectionJob, response.json()["job_id"])
        actions = set(db.scalars(select(AuditEvent.action)))
        runs = db.execute(select(func.count()).select_from(CollectionRun)).scalar_one()
    assert job.kind == DISCOVER_SECURITY_POLICIES
    assert actions == {"gitlab.security_policy_discovery_requested"}
    assert runs == 0


@pytest.mark.db
def test_security_policy_runs_are_listed_by_kind(migrated_engine: Engine, gitlab_env: None) -> None:
    seed(migrated_engine)
    fake = discovered(migrated_engine)

    body = (
        client_as(migrated_engine, fake, Role.AUDITOR)
        .get("/api/v1/gitlab/runs", params={"kind": "security_policies"})
        .json()
    )

    assert [item["kind"] for item in body["items"]] == ["security_policies"]
