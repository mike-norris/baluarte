"""The auditor report (PDF): the endpoint, its content, escaping, caps, determinism and audit."""

import hashlib
import io
import json
import re
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session
from test_change_evidence import Changes, gitlab_env, package, period, stamp
from test_exceptions import user
from test_findings_api import client_as

from baluarte.audit import details_digest
from baluarte.auth.roles import Role
from baluarte.compliance import attestations
from baluarte.compliance import exceptions as compliance_exceptions
from baluarte.compliance.exceptions import Actor
from baluarte.controls.changes import Proposal, propose
from baluarte.db.models import (
    AuditEvent,
    GitLabGroup,
    GitLabProject,
    PolicyException,
    ScopeExclusion,
)
from baluarte.evidence import report
from baluarte.evidence import verify as verify_tool
from baluarte.evidence.report import Capped, ControlRow, NotMet, ReportData, render
from baluarte.workspaces import default_workspace_id, workspace_of

pytestmark = pytest.mark.db

__all__ = ["gitlab_env"]

HOSTILE = "<b>bold</b> & <a href='https://example.test/x'>link</a> <font size=40>big</font>"
NON_LATIN = "Ελληνικά Кириллица"
TOKEN = "glpat-" + "a1B2c3D4e5F6g7H8i9J0"


@pytest.fixture
def world(migrated_engine: Engine) -> Changes:
    world = Changes(migrated_engine)
    world.collect()
    return world


def post_report(engine: Engine, role: Role = Role.AUDITOR, **body: Any) -> Any:
    client: TestClient = client_as(engine, role)
    return client.post("/api/v1/evidence/reports", json=body, headers={"X-CSRF-Token": "csrf"})


def pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def flat(value: str) -> str:
    """Text without line breaks, so wrapped table cells read as one string."""
    return re.sub(r"\s+", " ", value)


def squash(value: str) -> str:
    """Text without any whitespace, for values a narrow table cell may break inside a word."""
    return re.sub(r"\s+", "", value)


def exclude(world: Changes, path: str, justification: str, gitlab_id: int) -> None:
    with Session(world.engine) as db:
        db.add(
            GitLabProject(
                instance_id=world.instance_id,
                gitlab_id=gitlab_id,
                namespace_gitlab_id=4,
                namespace_kind="group",
                path_with_namespace=path,
                name=path.rsplit("/", 1)[-1],
                visibility="private",
                default_branch="main",
            )
        )
        db.add(
            ScopeExclusion(
                workspace_id=default_workspace_id(db),
                instance_id=world.instance_id,
                target_type="project",
                target_id=gitlab_id,
                target_path=path,
                batch_id=f"b{gitlab_id}",
                justification=justification,
                state="active",
                review_required=False,
                self_approved=True,
                proposed_by="user:1",
                proposed_by_user_id=1,
                decided_by="user:1",
                decided_by_user_id=1,
                decided_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        db.commit()


# The endpoint


def test_the_report_is_a_pdf_with_every_section(world: Changes) -> None:
    world.run()
    window = period()

    response = post_report(world.engine, framework="soc2", **window)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    stamp_from = window["from"].replace("-", "").replace(":", "").split(".")[0].rstrip("Z") + "Z"
    stamp_to = window["to"].replace("-", "").replace(":", "").split(".")[0].rstrip("Z") + "Z"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="baluarte-soc2-{stamp_from}-{stamp_to}.pdf"'
    )
    assert response.content.startswith(b"%PDF-")
    report_id = response.headers["X-Baluarte-Report-Id"]
    content = flat(pdf_text(response.content))
    for heading in (
        "Auditor report",
        "SOC 2 Type II",
        "Summary",
        "Controls by status",
        "Collection freshness at the period's end",
        "Controls",
        "Not met",
        "Exceptions",
        "Organization mapping decisions",
        "Attestations",
        "Monitoring scope",
        "Appendix: versions and verification",
        "How to verify",
    ):
        assert heading in content, heading
    assert f"Report ID {report_id}" in content
    assert "Requested by user:1" in content
    assert "not a SOC 2 certification" in content
    assert "CC8.1" in content
    assert "change-management" in content
    assert "GitLab instance gitlab.example.test " in content
    reader = PdfReader(io.BytesIO(response.content))
    pages = len(reader.pages)
    assert f"Page 1 of {pages}" in pdf_text(response.content)
    assert f"Page {pages} of {pages}" in pdf_text(response.content)
    assert reader.metadata is not None
    assert reader.metadata.creation_date_raw == "D:20000101000000+00'00'"


def test_the_cover_names_the_host_only() -> None:
    assert report._host("https://reader:secret@gitlab.example.test:8443/sub?x=1") == (
        "gitlab.example.test:8443"
    )
    assert report._host("https://gitlab.example.test") == "gitlab.example.test"


def test_the_report_needs_evidence_export(world: Changes) -> None:
    world.run()

    analyst = post_report(world.engine, Role.ANALYST, framework="soc2", **period())
    admin = post_report(world.engine, Role.ADMIN, framework="soc2", **period())

    assert analyst.status_code == 403
    assert admin.status_code == 200


def test_requests_are_validated_as_packages_are(world: Changes) -> None:
    early = post_report(
        world.engine,
        framework="soc2",
        **period(days=60) | {"to": stamp(datetime.now(UTC) - timedelta(days=30))},
    )
    world.run()
    future = post_report(
        world.engine,
        framework="soc2",
        **{"from": stamp(datetime.now(UTC)), "to": stamp(datetime.now(UTC) + timedelta(days=1))},
    )
    backwards = post_report(
        world.engine, framework="soc2", **{"from": period()["to"], "to": period()["from"]}
    )
    unknown_framework = post_report(world.engine, framework="pci", **period())
    unknown_group = post_report(world.engine, framework="soc2", groups=["nowhere"], **period())
    slashed = post_report(world.engine, framework="soc2", groups=["/acme"], **period())
    extra = post_report(world.engine, framework="soc2", sample={"size": 1}, **period())

    assert early.status_code == 409
    assert early.json()["detail"]["code"] == "no_evaluation_run"
    assert early.json()["detail"]["reasons"]
    assert future.status_code == 409
    assert future.json()["detail"]["code"] == "period_not_over"
    assert backwards.status_code == 422
    assert unknown_framework.status_code == 422
    assert unknown_group.status_code == 422
    assert "nowhere" in unknown_group.json()["detail"]
    assert slashed.status_code == 422
    assert extra.status_code == 422


def test_groups_narrow_the_report(world: Changes) -> None:
    with Session(world.engine) as db:
        db.add(
            GitLabGroup(instance_id=world.instance_id, gitlab_id=5, full_path="beta", name="beta")
        )
        db.add(
            GitLabProject(
                instance_id=world.instance_id,
                gitlab_id=9001,
                namespace_gitlab_id=5,
                namespace_kind="group",
                path_with_namespace="beta/api",
                name="api",
                visibility="private",
                default_branch="main",
            )
        )
        db.add(
            GitLabProject(
                instance_id=world.instance_id,
                gitlab_id=9002,
                namespace_gitlab_id=5,
                namespace_kind="group",
                path_with_namespace="beta/old",
                name="old",
                visibility="private",
                default_branch="main",
                archived=True,
            )
        )
        db.commit()
    world.run()
    window = period()

    everything = flat(pdf_text(post_report(world.engine, framework="soc2", **window).content))
    narrowed_response = post_report(world.engine, framework="soc2", groups=["acme"], **window)
    narrowed = flat(pdf_text(narrowed_response.content))

    # The archived project is counted in scope but never evaluated.
    assert "Projects in monitoring scope: 3, of which 1 archived" in everything
    assert "Projects evaluated in this run: 2." in everything
    assert "Projects in monitoring scope: 1, of which 0 archived" in narrowed
    assert "Projects evaluated in this run: 1." in narrowed
    assert "Scope acme" in narrowed
    assert "beta/api" not in narrowed
    with Session(world.engine) as db:
        created = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "evidence.report_created",
                AuditEvent.resource_id == narrowed_response.headers["X-Baluarte-Report-Id"],
            )
        ).one()
    assert created.details["scope"] == {"groups": ["acme"]}


# Content


def test_hostile_and_non_latin_text_is_shown_as_text(world: Changes) -> None:
    exclude(world, f"acme/{NON_LATIN} {HOSTILE}", f"Out of scope: {HOSTILE} {NON_LATIN}", 9100)
    exclude(world, "acme/tokens", f"Mentions a token {TOKEN} by mistake.", 9101)
    with Session(world.engine) as db:
        propose(
            db,
            Proposal(
                framework="soc2",
                control_id="CC6.6",
                kind="not_applicable",
                justification=f"No external access: {HOSTILE}",
                review_by=date.today() + timedelta(days=90),
            ),
            Actor(user_id=7, actor="user:7"),
        )
        attestations.record(
            db,
            "access-review",
            f"Reviewed every group. {HOSTILE} {NON_LATIN}",
            Actor(user_id=8, actor="user:8"),
        )
        db.commit()
    world.run()

    response = post_report(world.engine, framework="soc2", **period())

    assert response.status_code == 200, response.text
    content = flat(pdf_text(response.content))
    assert flat(f"Out of scope: {HOSTILE} {NON_LATIN}") in content
    assert NON_LATIN in content
    assert "<b>bold</b>" in content
    assert "href='https://example.test/x'" in content
    assert f"No external access: {HOSTILE}" in content
    assert "Not applicable" in content
    assert "user:7" in content
    assert "user:8" in content
    assert "access-review" in content
    assert TOKEN not in content
    assert "Mentions a token" in content
    assert "Projects excluded from monitoring: 2." in content


def test_exceptions_in_force_in_the_period_are_listed(world: Changes) -> None:
    requester, approver = user(world.engine, "ana"), user(world.engine, "bea")
    with Session(world.engine) as db:
        project_id = db.scalars(
            select(GitLabProject.id).where(GitLabProject.path_with_namespace == "acme/web")
        ).one()
        now = db.execute(select(func.now())).scalar_one()
        db.execute(
            insert(PolicyException).values(
                workspace_id=workspace_of(db, world.instance_id),
                instance_id=world.instance_id,
                project_id=project_id,
                policy_name="change-management",
                rule_id="changes-reviewed",
                justification=f"Hotfix process approved by the board {HOSTILE}",
                state="approved",
                requested_by_user_id=requester,
                requested_at=now - timedelta(days=2),
                expires_at=now + timedelta(days=30),
                decided_by_user_id=approver,
                decided_at=now - timedelta(days=1),
            )
        )
        compliance_exceptions.request(
            db,
            "audit-retention",
            "Retention lowered while storage is migrated.",
            date.today() + timedelta(days=30),
            Actor(user_id=9, actor="user:9"),
        )
        db.commit()
    world.run()

    content = squash(pdf_text(post_report(world.engine, framework="soc2", **period()).content))

    assert squash(f"Hotfix process approved by the board {HOSTILE}") in content
    assert squash("change-management / changes-reviewed") in content
    assert squash(f"by user:{approver}") in content
    assert squash("Operating exception") in content
    assert squash("audit-retention (CC7.2)") in content
    assert squash("Retention lowered while storage is migrated.") in content
    assert squash("by user:9") in content


def test_render_escapes_every_value() -> None:
    hostile = f"{HOSTILE} {NON_LATIN} 日本"
    data = sample_data(hostile)

    content = flat(pdf_text(render(data)))

    # Every cell, heading and cover value shows the markup as text.
    assert content.count("<b>bold</b>") >= 10
    assert "<font size=40>big</font>" in content


def test_long_identifiers_break_after_punctuation_not_mid_word() -> None:
    report._register_fonts()
    style = report._styles().cell
    lines = report._wrap_token("gitlab.project.merge_request_approvals", 90, style.fontName, 7.5)

    assert len(lines) > 1
    assert "".join(lines) == "gitlab.project.merge_request_approvals"
    assert all(line[-1] in report.BREAK_AFTER for line in lines[:-1])
    # Short values, and tokens with nowhere to break, are left as they are.
    assert report._wrap_token("pass", 90, style.fontName, 7.5) == ["pass"]
    assert report._fit_cell("a b", 90, style) == "a b"


def test_the_statement_starts_its_sentences_with_capitals() -> None:
    assert ". unknown" not in report.STATEMENT


def test_text_escapes_markup() -> None:
    assert report.text("<b>x</b> & <a href='y'>") == "&lt;b&gt;x&lt;/b&gt; &amp; &lt;a href='y'&gt;"
    assert report.text("one\ntwo\x00") == "one<br/>two"


def test_detail_tables_are_capped(world: Changes, monkeypatch: pytest.MonkeyPatch) -> None:
    for number in range(3):
        exclude(
            world,
            f"acme/excluded-{number}",
            f"Excluded number {number} for a reason.",
            9200 + number,
        )
    world.run()
    monkeypatch.setattr(report, "MAX_ROWS", 2)

    content = flat(pdf_text(post_report(world.engine, framework="soc2", **period()).content))

    assert "And 1 more; see the evidence package." in content
    assert "Excluded number 0" in content
    assert "Excluded number 2" not in content


def test_failing_rows_are_capped_per_control() -> None:
    failing = Capped([["acme/a", "baseline v1", "rule", "gitlab.change.reviewed"]], 31)
    data = replace(
        sample_data("x"),
        not_met=[NotMet("CC8.1", "Changes are authorized", failing)],
    )

    content = flat(pdf_text(render(data)))

    assert "And 30 more; see the evidence package." in content


def test_empty_sections_say_none(world: Changes) -> None:
    world.run()

    content = flat(pdf_text(post_report(world.engine, framework="iso27001", **period()).content))

    assert content.count("None.") >= 3
    assert "ISO/IEC 27001:2022" in content


def test_the_same_evidence_gives_the_same_report(world: Changes) -> None:
    world.run()
    window = period()

    first = post_report(world.engine, framework="soc2", **window)
    second = post_report(world.engine, framework="soc2", **window)

    def normalized(response: Any) -> str:
        content = pdf_text(response.content).replace(response.headers["X-Baluarte-Report-Id"], "ID")
        return re.sub(
            r"\d{4}-\d{2}-\d{2}T[\d:.]+Z \(database clock", "TIME (database clock", content
        )

    assert first.headers["X-Baluarte-Report-Id"] != second.headers["X-Baluarte-Report-Id"]
    assert normalized(first) == normalized(second)


def test_rendering_is_byte_for_byte_deterministic() -> None:
    data = sample_data(NON_LATIN)

    assert render(data) == render(data)
    assert render(data) != render(replace(data, report_id="f" * 32))


def test_the_report_is_audited_with_its_digest(world: Changes) -> None:
    world.run()
    window = period()

    response = post_report(world.engine, framework="soc2", groups=["acme"], **window)

    report_id = response.headers["X-Baluarte-Report-Id"]
    with Session(world.engine) as db:
        events = list(
            db.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_type == "evidence_report")
                .where(AuditEvent.resource_id == report_id)
                .order_by(AuditEvent.id)
            )
        )
    assert [event.action for event in events] == [
        "evidence.report_created",
        "evidence.report_completed",
    ]
    created, completed = events
    assert created.actor == "user:1"
    assert created.details["framework"] == "soc2"
    assert (created.details["from"], created.details["to"]) == (window["from"], window["to"])
    assert created.details["evaluation_run_id"] is not None
    assert completed.details == {
        "bytes": len(response.content),
        "sha256": hashlib.sha256(response.content).hexdigest(),
    }


# In the evidence package


def test_the_package_can_include_the_report(world: Changes, tmp_path: Path) -> None:
    world.run()

    response = package(world.engine, **period(), sample={"size": 1, "seed": 5}, report="soc2")

    assert response.status_code == 200, response.text
    export_id = response.headers["X-Baluarte-Export-Id"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    pdf = archive.read("report.pdf")
    manifest = json.loads(archive.read("manifest.json"))
    listed = {entry["name"]: entry for entry in manifest["files"]}
    assert listed["report.pdf"]["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert listed["report.pdf"]["bytes"] == len(pdf)
    assert f"{hashlib.sha256(pdf).hexdigest()}  report.pdf" in archive.read("SHA256SUMS").decode()
    assert manifest["scope"]["report"] == "soc2"
    assert manifest["manifest_format"] == "baluarte.evidence-manifest/v7"
    assert "report.pdf" in archive.read("README.txt").decode()
    content = flat(pdf_text(pdf))
    assert f"Report ID {export_id}" in content
    stored = tmp_path / "package.zip"
    stored.write_bytes(response.content)
    result, _ = verify_tool.verify(str(stored), details_digest(manifest))
    assert result.failures == []


def test_including_the_report_needs_evidence_export(world: Changes) -> None:
    world.run()

    analyst = package(world.engine, Role.ANALYST, **period(), sample={"size": 1}, report="soc2")
    plain = package(world.engine, Role.ANALYST, **period(), sample={"size": 1})

    assert analyst.status_code == 403
    assert plain.status_code == 200


def test_a_package_without_the_report_has_no_pdf(world: Changes) -> None:
    world.run()

    response = package(world.engine, **period(), sample={"size": 1, "seed": 5})

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert "report.pdf" not in archive.namelist()
    assert "report.pdf" not in archive.read("README.txt").decode()


def sample_data(value: str) -> ReportData:
    """Report data with ``value`` in every free-text field."""
    row = [value] * 7
    return ReportData(
        report_id="0" * 32,
        generated_at="2026-10-01T00:00:00Z",
        framework="soc2",
        framework_name="SOC 2 Type II",
        period_from="2026-07-01T00:00:00Z",
        period_to="2026-10-01T00:00:00Z",
        instance_host="gitlab.example.test",
        run_id=1,
        run_finished_at="2026-09-30T23:00:00Z",
        baluarte_version="0.0.0",
        requested_by="user:1",
        groups=[value],
        by_status={status: 0 for status in report.STATUSES},
        rule_types_evaluated=1,
        rule_types_total=2,
        projects_monitored=1,
        projects_archived=0,
        projects_evaluated=1,
        projects_excluded=0,
        freshness=[["projects", "fresh", "2026-09-30T22:00:00Z"]],
        controls=[ControlRow("CC8.1", value, "not_met", [value], 1, 2, 3, 4)],
        not_met=[NotMet("CC8.1", value, Capped([row[:4]], 1))],
        exceptions=Capped([row[:5]], 1),
        decisions=Capped([row[:6]], 1),
        attestations=Capped([row[:5]], 1),
        exclusions=Capped([row[:4]], 1),
        policies=Capped([row[:3]], 1),
        mapping_version="2026.11.1",
        mapping_changes=0,
        population=value,
    )
