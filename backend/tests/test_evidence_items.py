"""Evidence items (roadmap M15.2): uploads and references with controls, review by a second
person, freshness, restriction, reference checks and retention."""

import hashlib
import io
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from oidc_fake import make_settings
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import (
    Principal,
    auth_settings,
    current_principal,
    database_engine,
)
from baluarte.auth.roles import Role
from baluarte.compliance import retention
from baluarte.db.models import (
    MAX_EVIDENCE_FILE_BYTES,
    AuditEvent,
    CapturedFile,
    EvidenceItem,
    EvidenceItemControl,
)

pytestmark = pytest.mark.db

ITEMS = "/api/v1/evidence-items"
PDF = b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
CC81 = {"framework": "soc2", "control_id": "CC8.1"}
ANA, BEA, CY = 1, 2, 3


def client(engine: Engine, user_id: int, *roles: Role) -> TestClient:
    from baluarte.main import create_app

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
    return TestClient(app, headers={"X-CSRF-Token": "csrf"})


def analyst(engine: Engine, user_id: int = ANA) -> TestClient:
    return client(engine, user_id, Role.ANALYST)


def auditor(engine: Engine, user_id: int = BEA) -> TestClient:
    return client(engine, user_id, Role.AUDITOR)


def add(api: TestClient, **body: Any) -> dict[str, Any]:
    response = api.post(
        ITEMS, json={"kind": "file", "title": "Change board minutes", "controls": [CC81], **body}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def upload(api: TestClient, item_id: int, content: bytes = PDF, name: str = "minutes.pdf") -> Any:
    return api.put(
        f"{ITEMS}/{item_id}/file",
        params={"filename": name},
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )


def submitted(engine: Engine, **body: Any) -> dict[str, Any]:
    api = analyst(engine)
    item = add(api, **body)
    if item["kind"] == "file":
        assert upload(api, item["id"]).status_code == 200
    response = api.post(f"{ITEMS}/{item['id']}/submit")
    assert response.status_code == 200, response.text
    return dict(response.json())


def accepted(engine: Engine, **body: Any) -> dict[str, Any]:
    item = submitted(engine, **body)
    response = auditor(engine).post(f"{ITEMS}/{item['id']}/accept", json={})
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_a_file_is_added_submitted_and_accepted_by_someone_else(migrated_engine: Engine) -> None:
    api = analyst(migrated_engine)
    item = add(api, description="Minutes of the October change board.", valid_until="2099-01-01")
    assert item["state"] == "draft"
    assert item["controls"] == [
        {"framework": "soc2", "version": "2017-r2022", "control_id": "CC8.1"}
    ]

    assert api.post(f"{ITEMS}/{item['id']}/submit").json()["detail"]["code"] == "no_file"
    stored = upload(api, item["id"])
    assert stored.status_code == 200, stored.text
    assert stored.json()["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert stored.json()["content_type"] == "application/pdf"
    assert api.post(f"{ITEMS}/{item['id']}/submit").json()["state"] == "submitted"
    # Nothing changes once submitted.
    assert upload(api, item["id"]).status_code == 409
    assert api.post(f"{ITEMS}/{item['id']}/accept", json={}).status_code == 403

    reviewer = auditor(migrated_engine)
    rejected = reviewer.post(f"{ITEMS}/{item['id']}/reject", json={})
    assert rejected.status_code == 422
    done = reviewer.post(f"{ITEMS}/{item['id']}/accept", json={"note": "Matches the CAB log."})
    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["state"], body["reviewed_by"]["user_id"], body["freshness"]) == (
        "accepted",
        BEA,
        "fresh",
    )
    downloaded = reviewer.get(f"{ITEMS}/{item['id']}/file")
    assert downloaded.content == PDF
    assert downloaded.headers["content-disposition"] == 'attachment; filename="minutes.pdf"'
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    with Session(migrated_engine) as db:
        actions = db.scalars(
            select(AuditEvent.action)
            .where(AuditEvent.resource_type == "evidence_item")
            .order_by(AuditEvent.id)
        ).all()
    assert actions == [
        "evidence_item.added",
        "evidence_item.file_attached",
        "evidence_item.submitted",
        "evidence_item.accepted",
    ]


def test_a_reviewer_never_reviews_their_own_outside_development_mode(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer = auditor(migrated_engine)
    item = add(reviewer)
    assert upload(reviewer, item["id"]).status_code == 200
    assert reviewer.post(f"{ITEMS}/{item['id']}/submit").status_code == 200
    assert reviewer.post(f"{ITEMS}/{item['id']}/accept", json={}).status_code == 403

    monkeypatch.setenv("BALUARTE_DEVELOPMENT_MODE", "true")
    done = reviewer.post(f"{ITEMS}/{item['id']}/accept", json={})
    assert done.status_code == 200, done.text
    assert done.json()["development_review"] is True


def office(part: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{part}document.xml", "<doc/>")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("content", "name", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"0" * 20, "screen.png", "image/png"),
        (b"\xff\xd8\xff\xe0" + b"0" * 20, "photo.jpg", "image/jpeg"),
        (b"date,change\n2026-10-01,CAB-12\n", "log.csv", "text/csv"),
        (b'{"approved": true}', "export.json", "application/json"),
        (b"Plain notes.\n", "notes.txt", "text/plain"),
        (
            office("word/"),
            "policy.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (b"MZ\x90\x00\x03\x00", "tool.exe", None),
        (b"{not json", "export.json", None),
        (office("xl/"), "policy.docx", None),
        (b"<script>alert(1)</script>", "page.html", None),
    ],
)
def test_only_allowed_file_types_are_stored(
    migrated_engine: Engine, content: bytes, name: str, expected: str | None
) -> None:
    api = analyst(migrated_engine)
    item = add(api)
    response = upload(api, item["id"], content, name)
    if expected is None:
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "file_type"
    else:
        assert response.status_code == 200, response.text
        assert response.json()["content_type"] == expected


def test_a_file_over_25_mib_is_refused(migrated_engine: Engine) -> None:
    api = analyst(migrated_engine)
    item = add(api)
    assert upload(api, item["id"], b"%PDF-" + b"0" * MAX_EVIDENCE_FILE_BYTES).status_code == 413
    assert upload(api, item["id"], b"").status_code == 422


def test_decided_items_and_their_links_never_change(migrated_engine: Engine) -> None:
    item = accepted(migrated_engine)
    for statement in (
        "UPDATE evidence_item SET title = 'Something else' WHERE id = :id",
        "UPDATE evidence_item SET state = 'rejected', review_note = 'x' WHERE id = :id",
        "DELETE FROM evidence_item WHERE id = :id",
        "DELETE FROM evidence_item_control WHERE evidence_item_id = :id",
    ):
        with pytest.raises(DBAPIError, match=r"cannot|only"):
            with migrated_engine.begin() as connection:
                connection.execute(text(statement), {"id": item["id"]})

    api = analyst(migrated_engine)
    edited = api.put(f"{ITEMS}/{item['id']}", json={"title": "Changed", "controls": [CC81]})
    assert edited.status_code == 409
    draft = add(api)
    assert analyst(migrated_engine, CY).delete(f"{ITEMS}/{draft['id']}").status_code == 403
    assert api.delete(f"{ITEMS}/{draft['id']}").status_code == 204
    with Session(migrated_engine) as db:
        assert db.get(EvidenceItem, draft["id"]) is None
        assert not db.scalars(
            select(EvidenceItemControl).where(EvidenceItemControl.evidence_item_id == draft["id"])
        ).all()


def test_restricted_items_are_seen_only_by_reviewers_and_whoever_added_them(
    migrated_engine: Engine,
) -> None:
    item = submitted(migrated_engine, restricted=True)
    other = analyst(migrated_engine, CY)
    assert other.get(f"{ITEMS}/{item['id']}").status_code == 404
    assert other.get(f"{ITEMS}/{item['id']}/file").status_code == 404
    assert item["id"] not in {i["id"] for i in other.get(ITEMS).json()["items"]}
    assert analyst(migrated_engine).get(f"{ITEMS}/{item['id']}").status_code == 200
    assert auditor(migrated_engine).get(f"{ITEMS}/{item['id']}").status_code == 200


def test_items_are_listed_by_control_and_state(migrated_engine: Engine) -> None:
    change = accepted(migrated_engine)
    access = submitted(
        migrated_engine, controls=[{"framework": "iso27001", "control_id": "A.5.15"}]
    )
    api = auditor(migrated_engine)
    by_control = api.get(ITEMS, params={"control": "soc2:CC8.1"}).json()["items"]
    assert [i["id"] for i in by_control] == [change["id"]]
    pending = api.get(ITEMS, params={"state": "submitted"}).json()["items"]
    assert [i["id"] for i in pending] == [access["id"]]
    assert api.get(ITEMS, params={"control": "nonsense"}).status_code == 422
    unknown = analyst(migrated_engine).post(
        ITEMS,
        json={
            "kind": "file",
            "title": "Unknown control",
            "controls": [{"framework": "soc2", "control_id": "CC9.9"}],
        },
    )
    assert unknown.status_code == 422


def test_a_correction_supersedes_an_accepted_item(migrated_engine: Engine) -> None:
    first = accepted(migrated_engine, valid_until=(date.today() - timedelta(days=1)).isoformat())
    assert first["freshness"] == "stale"
    second = accepted(migrated_engine, supersedes_id=first["id"], valid_until="2099-12-31")
    assert second["supersedes_id"] == first["id"]
    again = auditor(migrated_engine).get(f"{ITEMS}/{first['id']}").json()
    assert again["superseded_by_id"] == second["id"]
    draft = add(analyst(migrated_engine))
    refused = analyst(migrated_engine).post(
        ITEMS,
        json={"kind": "file", "title": "Correct a draft", "supersedes_id": draft["id"]},
    )
    assert refused.status_code == 409


REAL_CLIENT = httpx.Client


def responder(status: int, body: bytes = b"policy text") -> Callable[..., httpx.Client]:
    def make(**_: Any) -> httpx.Client:
        return REAL_CLIENT(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body))
        )

    return make


def test_references_are_checked_only_against_allowed_hosts(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = analyst(migrated_engine)
    wiki = add(api, kind="reference", url="https://docs.example.test/policies/change")
    plain = add(api, kind="reference", url="http://docs.example.test/policies/change")
    assert add(api, kind="reference", url="https://docs.example.test/x")["url"]
    assert api.post(ITEMS, json={"kind": "reference", "title": "No address"}).status_code == 422
    assert api.post(f"{ITEMS}/{wiki['id']}/check").json()["detail"]["code"] == "check_not_allowed"
    assert api.get(ITEMS).json()["checks_enabled"] is False

    monkeypatch.setenv("BALUARTE_EVIDENCE_CHECK_HOSTS", "docs.example.test")
    monkeypatch.setattr(httpx, "Client", responder(200))
    checked = api.post(f"{ITEMS}/{wiki['id']}/check")
    assert checked.status_code == 200, checked.text
    assert (checked.json()["check_status"], checked.json()["check_http_status"]) == (
        "reachable",
        200,
    )
    assert checked.json()["check_sha256"] == hashlib.sha256(b"policy text").hexdigest()
    assert api.post(f"{ITEMS}/{plain['id']}/check").status_code == 409

    monkeypatch.setattr(httpx, "Client", responder(404))
    gone = api.post(f"{ITEMS}/{wiki['id']}/check").json()
    assert (gone["check_status"], gone["check_http_status"]) == ("unreachable", 404)
    # A decided reference still records later checks.
    submit = api.post(f"{ITEMS}/{wiki['id']}/submit")
    assert submit.status_code == 200
    assert auditor(migrated_engine).post(f"{ITEMS}/{wiki['id']}/accept", json={}).status_code == 200
    monkeypatch.setattr(httpx, "Client", responder(200))
    assert api.post(f"{ITEMS}/{wiki['id']}/check").json()["check_status"] == "reachable"


def test_retention_removes_old_files_but_keeps_the_record(migrated_engine: Engine) -> None:
    old = accepted(migrated_engine, valid_until="2001-01-01")
    current = accepted(migrated_engine, valid_until="2099-01-01")
    with Session(migrated_engine) as db:
        details = retention.expire(db)
        db.commit()
    # Both items hold the same file, so the stored file stays for the current one.
    assert details["deleted"]["evidence_item_file"] == 1
    assert details["deleted"]["captured_file"] == 0
    api = auditor(migrated_engine)
    gone = api.get(f"{ITEMS}/{old['id']}").json()
    assert gone["file_removed_at"] is not None
    assert gone["sha256"] == old["sha256"]
    assert api.get(f"{ITEMS}/{old['id']}/file").status_code == 404
    assert api.get(f"{ITEMS}/{current['id']}/file").status_code == 200

    with migrated_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE evidence_item DISABLE TRIGGER evidence_item_transition")
        )
        connection.execute(
            text("UPDATE evidence_item SET valid_until = '2001-01-01' WHERE id = :id"),
            {"id": current["id"]},
        )
        connection.execute(
            text("ALTER TABLE evidence_item ENABLE TRIGGER evidence_item_transition")
        )
    with Session(migrated_engine) as db:
        details = retention.expire(db)
        db.commit()
        assert details["deleted"]["captured_file"] == 1
        assert db.get(CapturedFile, current["sha256"]) is None
