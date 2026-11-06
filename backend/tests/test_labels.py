"""Label collection, the taxonomy's review, the label rule, and label plans end to end."""

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL, LabelServer, settings
from sqlalchemy import Engine, delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_exceptions import client_as, user

from baluarte.api.v1.gitlab import gitlab_transport
from baluarte.auth.roles import Role
from baluarte.db.models import (
    AuditEvent,
    CollectionJob,
    CollectionRun,
    GitLabGroup,
    GitLabProject,
    LabelTaxonomy,
    Observation,
)
from baluarte.integrations.gitlab import labels as label_reads
from baluarte.integrations.gitlab.client import GitLabClient
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.jobs import COLLECT_LABELS, make_collect_labels, queue_labels
from baluarte.jobs.worker import Worker
from baluarte.labels import drift, taxonomy
from baluarte.policy import changes
from baluarte.policy.changes import Actor, ChangeConflict, NotAllowed
from baluarte.policy.evaluation import labels as label_rule
from baluarte.policy.evaluation.engine import ProjectFacts
from baluarte.policy.evaluation.runner import EVALUATE
from baluarte.policy.schema import LabelsConsistentRule

pytestmark = pytest.mark.db

TAXONOMY = """\
apiVersion: baluarte.io/v1alpha1
kind: LabelTaxonomy
metadata:
  name: acme-labels
spec:
  scope:
    groups: ["acme"]
  labels:
    - name: "priority::high"
      color: "#D9534F"
      description: "Must be fixed this sprint"
      aliases: ["P1"]
    - name: bug
      color: "#FF0000"
    - name: docs
      color: "#0000FF"
      description: "Documentation"
"""

ACME, TEAM, OTHER = ("group", 10), ("group", 11), ("group", 20)
WEB, API = ("project", 100), ("project", 101)


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("BALUARTE_LABEL_WRITES", raising=False)
    monkeypatch.delenv("BALUARTE_SECRET_KEY_PREVIOUS_FILE", raising=False)
    key = tmp_path / "secret-key"
    key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n")
    monkeypatch.setenv("BALUARTE_SECRET_KEY_FILE", str(key))


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    with Session(migrated_engine) as db:
        instance_id = instance_for(db, URL).id
        for gitlab_id, path, parent in (
            (10, "acme", None),
            (11, "acme/team", 10),
            (20, "other", None),
        ):
            db.add(
                GitLabGroup(
                    instance_id=instance_id,
                    gitlab_id=gitlab_id,
                    full_path=path,
                    name=path.rsplit("/", 1)[-1],
                    parent_gitlab_id=parent,
                )
            )
        for gitlab_id, path, namespace in ((100, "acme/team/web", 11), (101, "acme/api", 10)):
            db.add(
                GitLabProject(
                    instance_id=instance_id,
                    gitlab_id=gitlab_id,
                    namespace_gitlab_id=namespace,
                    namespace_kind="group",
                    path_with_namespace=path,
                    name=path.rsplit("/", 1)[-1],
                    visibility="private",
                    state="active",
                )
            )
        db.commit()
    return migrated_engine


@pytest.fixture
def people(engine: Engine) -> dict[str, int]:
    return {name: user(engine, name) for name in ("ana", "bea")}


@pytest.fixture
def server() -> LabelServer:
    found = LabelServer()
    found.define(ACME, "P1", "#d9534f", description="Must be fixed this sprint")
    found.define(ACME, "docs", "#000000", description="Documentation")
    found.define(TEAM, "bug", "#ff0000")
    return found


def instance_id(engine: Engine) -> int:
    with Session(engine) as db:
        return instance_for(db, URL).id


def collect(engine: Engine, server: LabelServer) -> label_reads.LabelCollection:
    with GitLabClient(settings(), transport=server.transport) as client:
        return label_reads.collect(client, engine, instance_id(engine))


def put_in_force(engine: Engine, by: int, source: str = TAXONOMY) -> int:
    with Session(engine) as db:
        row = taxonomy.propose(db, instance_id(engine), source, None, Actor(by, f"user:{by}")).row
        db.commit()
        return row.id


def require_review(engine: Engine) -> None:
    with Session(engine) as db:
        changes.set_review_required(db, True, "user:1")
        db.commit()


def as_user(engine: Engine, user_id: int, server: LabelServer, *roles: Role) -> TestClient:
    client = client_as(engine, user_id, *roles)
    client.app.dependency_overrides[gitlab_transport] = lambda: server.transport  # type: ignore[attr-defined]
    return client


def audit_actions(engine: Engine, prefix: str) -> list[tuple[str, dict[str, Any] | None]]:
    with Session(engine) as db:
        return [
            (row.action, row.details)
            for row in db.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.startswith(prefix))
                .order_by(AuditEvent.id)
            )
        ]


# --- Collection --------------------------------------------------------------------------------


def test_collection_reads_every_owner_with_get_and_records_unreadable_ones(
    engine: Engine, server: LabelServer
) -> None:
    server.read_status[WEB] = 403

    result = collect(engine, server)

    assert result.as_counts() == {
        "groups": {"ok": 3},
        "projects": {"forbidden": 1, "ok": 1},
        "labels": 3,
    }
    assert {method for method, _, _ in server.requests} == {"GET"}
    assert {token for _, _, token in server.requests} == {TOKEN}
    with Session(engine) as db:
        rows = db.scalars(select(Observation).where(Observation.source.in_(label_reads.SOURCES)))
        statuses = {row.source_ref: row.collection_status for row in rows}
    assert statuses == {
        "acme": "ok",
        "acme/team": "ok",
        "other": "ok",
        "acme/api": "ok",
        "acme/team/web": "forbidden",
    }

    # Collecting again with nothing changed records nothing new.
    collect(engine, server)
    with Session(engine) as db:
        count = db.execute(
            select(text("count(*)"))
            .select_from(Observation)
            .where(Observation.source.in_(label_reads.SOURCES))
        ).scalar_one()
    assert count == 5


def test_the_collection_job_records_a_labels_run_and_queues_an_evaluation(
    engine: Engine, server: LabelServer
) -> None:
    with Session(engine) as db:
        queue_labels(db, "after-run:1")
        db.commit()

    handler = make_collect_labels(server.transport)
    assert Worker(engine, {COLLECT_LABELS: handler}).run_once()

    with Session(engine) as db:
        run = db.scalars(select(CollectionRun).where(CollectionRun.kind == "labels")).one()
        kinds = set(db.scalars(select(CollectionJob.kind)))
    assert (run.status, run.seen, run.error) == ("succeeded", 5, None)
    assert run.counts["labels"] == 3
    assert EVALUATE in kinds


def test_a_rejected_token_stops_collection_and_the_rest_is_not_read(
    engine: Engine, server: LabelServer
) -> None:
    server.read_status[ACME] = 401

    result = collect(engine, server)

    assert result.rejected
    assert len(server.requests) == 1
    assert result.failed == 5


def test_drift_from_collected_labels(
    engine: Engine, server: LabelServer, people: dict[str, int]
) -> None:
    put_in_force(engine, people["ana"])
    server.read_status[WEB] = 403
    collect(engine, server)

    with Session(engine) as db:
        found = drift.load(db, instance_id(engine))

    assert [(i.owner.path, i.kind, i.label) for i in found.items] == [
        ("acme", "missing", "bug"),
        ("acme", "misnamed", "priority::high"),
        ("acme", "color", "docs"),
        ("acme/team", "misplaced", "bug"),
        ("acme/team/web", "unknown", None),
    ]


# --- The taxonomy and its review ---------------------------------------------------------------


def test_with_review_a_version_waits_for_someone_else(
    engine: Engine, people: dict[str, int]
) -> None:
    require_review(engine)
    ana, bea = (Actor(people[n], f"user:{people[n]}") for n in ("ana", "bea"))
    iid = instance_id(engine)
    with Session(engine) as db:
        proposed = taxonomy.propose(db, iid, TAXONOMY, "First version", ana)
        db.commit()
        assert proposed.row.state == "proposed"
        assert taxonomy.in_force(db, iid) is None
        # The same document again is the same proposal; a different one waits its turn.
        assert not taxonomy.propose(db, iid, TAXONOMY, None, ana).created
        with pytest.raises(ChangeConflict):
            taxonomy.propose(db, iid, TAXONOMY.replace("acme-labels", "other"), None, bea)
        with pytest.raises(NotAllowed):
            taxonomy.approve(db, proposed.row.id, ana, None)
        with pytest.raises(ValueError):
            taxonomy.reject(db, proposed.row.id, bea, " ")
        taxonomy.approve(db, proposed.row.id, bea, "Looks right")
        db.commit()
        current = taxonomy.in_force(db, iid)
        assert current is not None and current.version == 1 and not current.self_approved
    assert [a for a, _ in audit_actions(engine, "labels.taxonomy")] == [
        "labels.taxonomy_proposed",
        "labels.taxonomy_approved",
    ]


def test_the_database_keeps_decided_versions_as_they_are(
    engine: Engine, people: dict[str, int]
) -> None:
    taxonomy_id = put_in_force(engine, people["ana"])

    for statement in (
        update(LabelTaxonomy).where(LabelTaxonomy.id == taxonomy_id).values(state="rejected"),
        update(LabelTaxonomy).where(LabelTaxonomy.id == taxonomy_id).values(source_yaml="x"),
        delete(LabelTaxonomy).where(LabelTaxonomy.id == taxonomy_id),
    ):
        with Session(engine) as db, pytest.raises(DBAPIError):
            db.execute(statement)
            db.commit()


def test_the_taxonomy_api_validates_proposes_and_refuses_own_approval(
    engine: Engine, server: LabelServer, people: dict[str, int]
) -> None:
    require_review(engine)
    ana = as_user(engine, people["ana"], server, Role.ANALYST)
    bea = as_user(engine, people["bea"], server, Role.ANALYST)

    invalid = ana.post("/api/v1/labels/taxonomy/validate", json={"source_yaml": "kind: x"})
    assert invalid.json()["valid"] is False and invalid.json()["errors"]
    valid = ana.post("/api/v1/labels/taxonomy/validate", json={"source_yaml": TAXONOMY}).json()
    assert valid["valid"] and valid["name"] == "acme-labels"
    refused = ana.post("/api/v1/labels/taxonomy", json={"source_yaml": "kind: x"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "invalid_taxonomy"

    created = ana.post(
        "/api/v1/labels/taxonomy", json={"source_yaml": TAXONOMY, "justification": "Start"}
    )
    assert created.status_code == 201
    version = created.json()
    assert version["state"] == "proposed" and version["proposed_by"]["display_name"] == "Ana"
    own = ana.post(f"/api/v1/labels/taxonomy/{version['id']}/approve", json={})
    assert own.status_code == 403 and own.json()["detail"]["code"] == "own_change"
    approved = bea.post(f"/api/v1/labels/taxonomy/{version['id']}/approve", json={})
    assert approved.json()["state"] == "approved"

    body = ana.get("/api/v1/labels/taxonomy").json()
    assert body["current"]["id"] == version["id"]
    assert body["review_required"] is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/labels/taxonomy"),
        ("post", "/api/v1/labels/plans"),
        ("post", "/api/v1/labels/plans/1/approve"),
        ("put", "/api/v1/settings/label-write-token"),
    ],
)
def test_auditors_cannot_change_labels(
    engine: Engine, server: LabelServer, people: dict[str, int], method: str, path: str
) -> None:
    auditor = as_user(engine, people["ana"], server, Role.AUDITOR)
    body = {"source_yaml": TAXONOMY, "plan_digest": "0" * 64, "token": "x", "scope": {}}

    assert getattr(auditor, method)(path, json=body).status_code == 403


def test_analysts_cannot_approve_plans(
    engine: Engine, server: LabelServer, people: dict[str, int]
) -> None:
    analyst = as_user(engine, people["ana"], server, Role.ANALYST)

    response = analyst.post("/api/v1/labels/plans/1/approve", json={"plan_digest": "0" * 64})

    assert response.status_code == 403


# --- Drift API and the label rule --------------------------------------------------------------


def test_drift_and_owner_detail_api(
    engine: Engine, server: LabelServer, people: dict[str, int]
) -> None:
    put_in_force(engine, people["ana"])
    collect(engine, server)
    auditor = as_user(engine, people["bea"], server, Role.AUDITOR)

    page = auditor.get("/api/v1/labels/drift", params={"limit": 2}).json()
    assert [item["kind"] for item in page["items"]] == ["missing", "misnamed"]
    assert page["next_after"] == "group:10:misnamed:priority::high"
    assert page["totals"]["total"] == 4
    assert page["totals"]["by_group"] == [
        {
            "group": "acme",
            "total": 4,
            "by_kind": {"missing": 1, "misnamed": 1, "color": 1, "misplaced": 1},
        }
    ]
    assert page["taxonomy"]["name"] == "acme-labels"
    rest = auditor.get("/api/v1/labels/drift", params={"after": page["next_after"]}).json()
    assert [item["kind"] for item in rest["items"]] == ["color", "misplaced"]
    assert rest["next_after"] is None

    web = auditor.get("/api/v1/labels/owners/project/100").json()
    assert web["status"] == "known"
    assert [(label["name"], label["defined_at"]["path"]) for label in web["labels"]] == [
        ("P1", "acme"),
        ("bug", "acme/team"),
        ("docs", "acme"),
    ]
    assert len(web["drift"]) == 4
    assert auditor.get("/api/v1/labels/owners/project/999").status_code == 404


def test_the_label_rule_judges_the_drift_reaching_a_project(
    engine: Engine, server: LabelServer, people: dict[str, int]
) -> None:
    rule = LabelsConsistentRule(id="labels", type="gitlab.labels.consistent", severity="low")
    lenient = LabelsConsistentRule(
        id="labels",
        type="gitlab.labels.consistent",
        severity="low",
        ignore=["missing", "misnamed", "color", "misplaced"],
    )

    def judged(project_gitlab_id: int) -> Any:
        with Session(engine) as db:
            row = db.scalars(
                select(GitLabProject).where(GitLabProject.gitlab_id == project_gitlab_id)
            ).one()
            facts = label_rule.facts(db, row)
        project = ProjectFacts(
            gitlab_id=row.gitlab_id,
            path=row.path_with_namespace,
            visibility=row.visibility,
            state=row.state,
            last_seen_at=row.last_seen_at,
            last_error=None,
            labels=facts,
        )
        return label_rule._observe(project)

    assert judged(101) is None  # no taxonomy in force
    put_in_force(engine, people["ana"])
    collect(engine, server)
    observed = judged(101)
    assert observed["kinds"] == ["color", "misnamed", "missing"]
    assert label_rule._judge(rule, observed)[0] is False
    assert label_rule._judge(lenient, observed)[0] is True
    assert len(observed["observations"]) == 2  # acme/api and acme

    server.read_status[TEAM] = 403
    collect(engine, server)
    assert judged(100) is None  # a group above it could not be read
