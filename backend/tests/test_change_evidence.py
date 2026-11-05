"""Change rules end to end, the Changes API, reading as of a moment and the evidence package."""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gitlab_fake import TOKEN, URL
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.orm import Session
from test_change_collection import PROJECT, busy_repository, sha
from test_findings_api import client_as

from baluarte.audit import details_digest
from baluarte.auth.roles import Role
from baluarte.changes.settings import ChangeEvidenceSettings
from baluarte.compliance.retention import expire
from baluarte.db.models import (
    AuditEvent,
    ChangeRecord,
    CollectionRun,
    Evaluation,
    EvaluationRun,
    GitLabGroup,
    GitLabProject,
    Observation,
    ScopeExclusion,
)
from baluarte.evidence import sampling
from baluarte.evidence import verify as verify_tool
from baluarte.integrations.gitlab import changes
from baluarte.integrations.gitlab.instances import instance_for
from baluarte.integrations.gitlab.settings import GitLabSettings
from baluarte.policy.assignments import Target, assign
from baluarte.policy.evaluation.runner import evaluate_all
from baluarte.policy.evaluation.store import combined_digest
from baluarte.policy.store import save_version

pytestmark = pytest.mark.db

SETTINGS = GitLabSettings(url=URL, token=TOKEN)
CHANGE_POLICY = """\
apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: change-management
spec:
  scope:
    groups: ["*"]
  rules:
    - id: changes-reviewed
      type: gitlab.change.reviewed
      window_days: 30
    - id: changes-tested
      type: gitlab.change.pipeline_passed
      window_days: 30
    - id: deployments-reviewed
      type: gitlab.change.deployed_after_review
      window_days: 30
"""
RULES = ("changes-reviewed", "changes-tested", "deployments-reviewed")


@pytest.fixture(autouse=True)
def gitlab_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "gitlab-token"
    token.write_text(TOKEN)
    monkeypatch.setenv("BALUARTE_GITLAB_URL", URL)
    monkeypatch.setenv("BALUARTE_GITLAB_TOKEN_FILE", str(token))
    monkeypatch.delenv("BALUARTE_GITLAB_TOKEN", raising=False)


class Changes:
    """An instance with one project in group acme, its changes collected and the change policy."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.repo = busy_repository()
        with Session(engine) as db:
            instance = instance_for(db, URL)
            self.instance_id = instance.id
            db.add(GitLabGroup(instance_id=instance.id, gitlab_id=4, full_path="acme", name="acme"))
            db.add(
                GitLabProject(
                    instance_id=instance.id,
                    gitlab_id=PROJECT,
                    namespace_gitlab_id=4,
                    namespace_kind="group",
                    path_with_namespace="acme/web",
                    name="web",
                    visibility="private",
                    default_branch="main",
                )
            )
            policy = save_version(db, CHANGE_POLICY).policy
            target = Target(target_type="instance", target_id=instance.id, path=URL, state=None)
            assign(db, instance.id, policy, target)
            db.commit()

    def collect(self) -> None:
        changes.collect(self.repo.client(), self.engine, self.instance_id, ChangeEvidenceSettings())
        with Session(self.engine) as db:
            for kind in ("projects", "changes"):
                db.execute(
                    insert(CollectionRun).values(
                        instance_id=self.instance_id,
                        kind=kind,
                        status="succeeded",
                        started_at=text("now()"),
                        finished_at=text("now()"),
                    )
                )
            db.commit()

    def run(self) -> EvaluationRun:
        run_id, _ = evaluate_all(self.engine, SETTINGS)
        with Session(self.engine) as db:
            return db.get_one(EvaluationRun, run_id)

    def results(self, run: EvaluationRun) -> dict[str, Evaluation]:
        with Session(self.engine) as db:
            return {
                e.rule_id: e
                for e in db.scalars(
                    select(Evaluation).where(Evaluation.evaluation_run_id == run.id)
                )
            }

    def record(self, kind: str, key: Any) -> ChangeRecord:
        with Session(self.engine) as db:
            return next(
                r
                for r in db.scalars(select(ChangeRecord))
                if changes.change_key(r.kind, r.iid, r.deployment_id, r.sha) == (kind, key)
            )


@pytest.fixture
def world(migrated_engine: Engine) -> Changes:
    world = Changes(migrated_engine)
    world.collect()
    return world


def get(engine: Engine, url: str, role: Role = Role.AUDITOR) -> Any:
    response = client_as(engine, role).get(url)
    assert response.status_code == 200, response.text
    return response.json()


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Rules


def test_change_rules_judge_the_window_and_rest_on_every_observation(world: Changes) -> None:
    run = world.run()

    results = world.results(run)
    reviewed = results["changes-reviewed"]
    assert reviewed.outcome == "fail"
    assert reviewed.observed is not None
    assert (reviewed.observed["changes"], reviewed.observed["failed"]) == (4, 2)
    assert {item["kind"] for item in reviewed.observed["failing"]} == {
        "merge_request",
        "direct_push",
    }
    tested = results["changes-tested"]
    assert (tested.outcome, tested.observed["failed"]) == ("fail", 1)  # type: ignore[index]
    deployed = results["deployments-reviewed"]
    assert deployed.outcome == "pass"

    with Session(world.engine) as db:
        evidence = [db.get_one(Observation, i) for i in reviewed.observation_ids or []]
        assert len(evidence) == 4
        assert {o.source for o in evidence} <= set(changes.CHANGE_SOURCES.values())
        assert reviewed.evidence_digest == combined_digest(evidence)
        # The deployment rule rests on the deployment and the change it shipped.
        assert len(deployed.observation_ids or []) == 2
        read = db.get_one(Observation, reviewed.observation_id)
        assert read.source == changes.CHANGE_READ_SOURCE


def test_tampered_change_evidence_is_never_a_result(world: Changes) -> None:
    record = world.record("merge_request", 2)
    with world.engine.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text(
                "UPDATE observation SET payload = jsonb_set(payload, "
                "'{approvals,approved_by,0,username}', '\"reviewer\"') WHERE id = :id"
            ),
            {"id": record.observation_id},
        )

    results = world.results(world.run())

    reviewed = results["changes-reviewed"]
    assert reviewed.outcome == "unknown"
    assert reviewed.unknown_reasons == ["evidence_digest_mismatch"]
    assert reviewed.observation_ids is None


def test_changes_never_read_are_unknown(migrated_engine: Engine) -> None:
    world = Changes(migrated_engine)

    results = world.results(world.run())

    assert {r.outcome for r in results.values()} == {"unknown"}


# Changes API


def test_changes_are_listed_newest_first_with_their_evidence(world: Changes) -> None:
    body = get(world.engine, "/api/v1/changes")

    kinds = [item["kind"] for item in body["items"]]
    assert sorted(kinds) == ["direct_push", "merge_request", "merge_request", "merge_request"]
    assert body["next_before_id"] is None
    assert body["as_of"] is None and body["evaluation_run"] is None
    first = next(item for item in body["items"] if item["iid"] == 1)
    assert first["project"] == {"gitlab_id": PROJECT, "path": "acme/web", "state": "active"}
    assert (first["outcome"], first["author"], first["merged_by"]) == ("pass", "dev", "lead")
    assert first["approvals"]["status"] == "known"
    assert [a["username"] for a in first["approvals"]["approved_by"]] == ["reviewer"]
    assert first["approvals"]["self_approved"] is False
    assert first["approvals"]["committer_approved"] is None
    assert first["pipeline"]["status"] == "known"
    assert first["pipeline"]["pipeline_status"] == "success"
    assert first["pipeline"]["tests"]["total"] == 3
    assert first["deployments"] == {
        "status": "known",
        "items": [
            {
                "environment": "production",
                "status": "success",
                "finished_at": first["deployments"]["items"][0]["finished_at"],
            }
        ],
    }
    assert first["web_path"] == "/acme/web/-/merge_requests/1"
    push = next(item for item in body["items"] if item["kind"] == "direct_push")
    assert push["approvals"] == {
        "status": "known",
        "approved_by": [],
        "required": None,
        "left": None,
        "self_approved": None,
        "committer_approved": None,
    }
    assert push["deployments"] == {"status": "known", "items": []}


def test_changes_of_excluded_projects_are_left_out_unless_named(world: Changes) -> None:
    with Session(world.engine) as db:
        db.add(
            ScopeExclusion(
                instance_id=world.instance_id,
                target_type="project",
                target_id=PROJECT,
                target_path="acme/web",
                batch_id="b",
                justification="Research project, not part of the audited product.",
                state="active",
                review_required=False,
                self_approved=True,
                proposed_by="user:1",
                proposed_by_user_id=1,
                decided_by="user:1",
                decided_by_user_id=1,
                decided_at=datetime.now(UTC),
            )
        )
        db.commit()

    assert get(world.engine, "/api/v1/changes")["items"] == []
    assert len(get(world.engine, f"/api/v1/changes?project={PROJECT}")["items"]) == 4


def test_changes_filters_and_pages(world: Changes) -> None:
    def ids(query: str) -> list[int]:
        return [i["id"] for i in get(world.engine, f"/api/v1/changes?{query}")["items"]]

    everything = ids("")
    assert len(ids("kind=deployment")) == 1
    assert len(ids("kind=merge_request")) == 3
    assert len(ids("outcome=fail")) == 2
    assert ids(f"project={PROJECT + 1}") == []
    page = get(world.engine, "/api/v1/changes?limit=3")
    rest = get(world.engine, f"/api/v1/changes?limit=3&before_id={page['next_before_id']}")
    assert [i["id"] for i in page["items"] + rest["items"]] == everything
    assert rest["next_before_id"] is None


def test_a_change_shows_every_version(world: Changes) -> None:
    world.repo.deploy(301, sha("m2"))
    world.collect()
    record = world.record("merge_request", 2)

    body = get(world.engine, f"/api/v1/changes/{record.id}")

    assert body["versions"][0]["observation_id"] == record.observation_id
    assert len(body["versions"]) == 2
    assert all(v["digest_valid"] for v in body["versions"])
    assert body["versions"][0]["payload"]["deployments"]["items"][0]["id"] == 301
    assert body["versions"][1]["payload"]["deployments"]["items"] == []
    assert client_as(world.engine, Role.AUDITOR).get("/api/v1/changes/999999").status_code == 404


def test_a_change_as_of_a_moment_before_a_later_version(world: Changes) -> None:
    record = world.record("merge_request", 2)
    with Session(world.engine) as db:
        moment = db.get_one(Observation, record.observation_id).collected_at
    world.repo.deploy(301, sha("m2"))
    world.collect()

    body = get(world.engine, f"/api/v1/changes/{record.id}?as_of={stamp(moment)}")
    listed = get(world.engine, f"/api/v1/changes?kind=deployment&as_of={stamp(moment)}")

    assert body["as_of"] is not None
    assert len(body["versions"]) == 1
    assert body["deployments"] == {"status": "known", "items": []}
    assert [item["id"] for item in listed["items"]] == [world.record("deployment", 300).id]
    early = stamp(moment - timedelta(days=1))
    assert (
        client_as(world.engine, Role.AUDITOR)
        .get(f"/api/v1/changes/{record.id}?as_of={early}")
        .status_code
        == 404
    )
    assert (
        client_as(world.engine, Role.AUDITOR)
        .get("/api/v1/changes?as_of=2026-11-01T00:00:00")
        .status_code
        == 422
    )


# As of


def test_results_as_of_a_moment_come_from_the_run_in_force_then(world: Changes) -> None:
    first = world.run()
    assert first.finished_at is not None
    world.repo.approvals[2] = {"approved_by": [{"user": {"username": "reviewer"}}]}
    world.collect()
    world.run()

    then = stamp(first.finished_at)
    findings = get(world.engine, f"/api/v1/findings?as_of={then}")
    current = get(world.engine, "/api/v1/findings")
    before = get(
        world.engine, f"/api/v1/findings?as_of={stamp(first.started_at - timedelta(days=1))}"
    )
    controls = get(world.engine, f"/api/v1/controls?as_of={then}")
    summary = get(world.engine, f"/api/v1/evaluations/summary?as_of={then}")
    project = get(world.engine, f"/api/v1/gitlab/projects/{PROJECT}/evaluations?as_of={then}")

    assert findings["as_of"] is not None
    assert findings["evaluation_run"]["id"] == first.id
    assert {f["rule_id"] for f in findings["items"] if f["state"] == "open"} == {
        "changes-reviewed",
        "changes-tested",
    }
    assert current["evaluation_run"]["id"] != first.id
    assert before == {
        "items": [],
        "next_after_id": None,
        "as_of": before["as_of"],
        "evaluation_run": None,
    }
    assert controls["evaluation_run"]["id"] == first.id
    assert summary["evaluation_run"]["id"] == first.id
    assert {e["rule_id"] for e in project["items"]} == set(RULES)
    assert all(e["evaluation_run_id"] == first.id for e in project["items"])


def test_a_finding_as_of_a_moment(world: Changes) -> None:
    first = world.run()
    assert first.finished_at is not None
    finding = next(
        f
        for f in get(world.engine, "/api/v1/findings")["items"]
        if f["rule_id"] == "changes-reviewed"
    )

    body = get(world.engine, f"/api/v1/findings/{finding['id']}?as_of={stamp(first.finished_at)}")
    history = get(
        world.engine,
        f"/api/v1/findings/{finding['id']}/evaluations?as_of={stamp(first.finished_at)}",
    )

    assert body["state"] == "open"
    assert body["evaluation_run"] == {
        "id": first.id,
        "finished_at": body["evaluation_run"]["finished_at"],
    }
    assert len(history["items"]) == 1
    assert history["items"][0]["observation_ids"]
    assert history["items"][0]["evidence_digest"]
    gone = client_as(world.engine, Role.AUDITOR).get(
        f"/api/v1/findings/{finding['id']}?as_of={stamp(first.started_at - timedelta(days=1))}"
    )
    assert gone.status_code == 404


# Evidence package


def package(engine: Engine, role: Role = Role.AUDITOR, **body: Any) -> Any:
    client: TestClient = client_as(engine, role)
    return client.post("/api/v1/evidence/packages", json=body, headers={"X-CSRF-Token": "csrf"})


def period(days: int = 30) -> dict[str, str]:
    now = datetime.now(UTC)
    return {"from": stamp(now - timedelta(days=days)), "to": stamp(now)}


def test_the_package_holds_results_changes_and_a_reproducible_sample(
    world: Changes, tmp_path: Path
) -> None:
    world.run()
    window = period()

    response = package(world.engine, **window, sample={"size": 2, "seed": 7})

    assert response.status_code == 200, response.text
    assert response.headers["X-Baluarte-Sample-Seed"] == "7"
    export_id = response.headers["X-Baluarte-Export-Id"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {
        "README.txt",
        "changes.csv",
        "change_sample.csv",
        "change_evidence.jsonl",
        "evaluations.csv",
        "findings.csv",
        "observations.jsonl",
        "manifest.json",
        "SHA256SUMS",
    } <= names
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["manifest_format"] == "baluarte.evidence-manifest/v6"
    assert manifest["export_format"] == "baluarte.evidence-package/v1"
    evidence_package = manifest["evidence_package"]
    sample = evidence_package["sample"]
    population = [
        world.record(kind, key).id
        for kind, key in (
            ("merge_request", 1),
            ("merge_request", 2),
            ("merge_request", 3),
            ("direct_push", sha("d")),
        )
    ]
    assert sample["population"] == 4
    assert sample["change_ids"] == sampling.draw(population, 2, 7)
    assert (sample["seed"], sample["seed_chosen_by"]) == (7, "requester")
    assert evidence_package["period"] == window
    evidence = [json.loads(line) for line in archive.read("change_evidence.jsonl").splitlines()]
    assert {row["change_id"] for row in evidence} == set(sample["change_ids"])
    assert all(row["digest_valid"] for row in evidence)
    readme = archive.read("README.txt").decode()
    assert "random.Random(seed).sample" in readme
    assert "seed 7" in readme

    stored = tmp_path / "package.zip"
    stored.write_bytes(response.content)
    report, _ = verify_tool.verify(str(stored), details_digest(manifest))
    assert report.failures == []

    again = package(world.engine, **window, sample={"size": 2, "seed": 7})
    repeated = json.loads(zipfile.ZipFile(io.BytesIO(again.content)).read("manifest.json"))
    assert repeated["evidence_package"]["sample"]["change_ids"] == sample["change_ids"]

    recorded = get(world.engine, f"/api/v1/evidence/packages/{export_id}")
    assert recorded["seed"] == 7
    assert recorded["sample_size"] == 2
    assert recorded["manifest_sha256"] == details_digest(manifest)
    assert recorded["from"] is not None and recorded["to"] is not None
    with Session(world.engine) as db:
        actions = list(
            db.scalars(select(AuditEvent.action).where(AuditEvent.resource_id == export_id))
        )
    assert actions == ["evidence.package_created", "evidence.package_completed"]


def test_a_package_without_a_seed_chooses_and_reports_one(world: Changes) -> None:
    world.run()

    response = package(world.engine, **period(), sample={"size": 1})

    assert response.status_code == 200, response.text
    seed = int(response.headers["X-Baluarte-Sample-Seed"])
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(response.content)).read("manifest.json"))
    assert manifest["evidence_package"]["sample"]["seed"] == seed
    assert manifest["evidence_package"]["sample"]["seed_chosen_by"] == "baluarte"


def test_a_tampered_sample_fails_verification(world: Changes, tmp_path: Path) -> None:
    world.run()
    response = package(world.engine, **period(), sample={"size": 2, "seed": 3})
    original = zipfile.ZipFile(io.BytesIO(response.content))
    stored = tmp_path / "edited.zip"
    with zipfile.ZipFile(stored, "w") as edited:
        for name in original.namelist():
            data = original.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["evidence_package"]["sample"]["seed"] = 4
                data = json.dumps(manifest).encode()
            edited.writestr(name, data)

    report, _ = verify_tool.verify(str(stored))

    assert any("redrawing" in failure for failure in report.failures)


def test_packages_that_cannot_be_built_conflict(world: Changes) -> None:
    early = package(
        world.engine,
        **period(days=60) | {"to": stamp(datetime.now(UTC) - timedelta(days=30))},
        sample={"size": 1},
    )
    future = package(
        world.engine,
        **{"from": stamp(datetime.now(UTC)), "to": stamp(datetime.now(UTC) + timedelta(days=1))},
        sample={"size": 1},
    )
    backwards = package(
        world.engine, **{"from": period()["to"], "to": period()["from"]}, sample={"size": 1}
    )

    assert early.status_code == 409
    assert early.json()["detail"]["code"] == "no_evaluation_run"
    assert early.json()["detail"]["reasons"]
    assert future.status_code == 409
    assert future.json()["detail"]["code"] == "period_not_over"
    assert backwards.status_code == 422
    assert package(world.engine, **period(), sample={"size": 501}).status_code == 422


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ANALYST, Role.AUDITOR])
def test_every_role_may_build_a_package(world: Changes, role: Role) -> None:
    world.run()

    assert package(world.engine, role, **period(), sample={"size": 1}).status_code == 200


# Settings


def test_change_evidence_settings_are_admin_only_and_audited(world: Changes) -> None:
    admin = client_as(world.engine, Role.ADMIN)
    analyst = client_as(world.engine, Role.ANALYST)
    body = {"lookback_days": 30, "redeploy_days": 3, "production_environments": ["prod", "live"]}

    assert analyst.get("/api/v1/settings/change-evidence").status_code == 403
    assert admin.get("/api/v1/settings/change-evidence").json() == {
        "lookback_days": 90,
        "redeploy_days": 7,
        "production_environments": ["production"],
    }
    saved = admin.put(
        "/api/v1/settings/change-evidence", json=body, headers={"X-CSRF-Token": "csrf"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == body
    duplicate = body | {"production_environments": ["prod", "prod"]}
    assert (
        admin.put(
            "/api/v1/settings/change-evidence", json=duplicate, headers={"X-CSRF-Token": "csrf"}
        ).status_code
        == 422
    )
    with Session(world.engine) as db:
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "settings.change_evidence_updated")
        ).one()
    assert event.details["previous"]["lookback_days"] == 90
    assert event.details["lookback_days"] == 30


# Retention


def backdate(world: Changes, record: ChangeRecord, days: int) -> None:
    with world.engine.begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        for table, column, row_id in (
            ("change_record", "changed_at", record.id),
            ("observation", "collected_at", record.observation_id),
        ):
            connection.execute(
                text(
                    f"UPDATE {table} SET {column} = {column} - make_interval(days => :days) "  # noqa: S608
                    "WHERE id = :id"
                ),
                {"days": days, "id": row_id},
            )


def test_retention_keeps_the_changes_a_result_rests_on(world: Changes) -> None:
    unused = world.record("merge_request", 3)
    backdate(world, unused, 5000)
    world.run()
    used = world.record("merge_request", 1)
    backdate(world, used, 5000)

    with Session(world.engine) as db:
        details = expire(db)
        db.commit()
        kept = set(db.scalars(select(ChangeRecord.id)))
        observations = set(db.scalars(select(Observation.id)))

    assert details["deleted"]["change_record"] == 1
    assert unused.id not in kept
    assert unused.observation_id not in observations
    assert used.id in kept
    assert used.observation_id in observations
