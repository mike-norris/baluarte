"""Backups: the runs the backup service reports, their posture, the services page and the
configuration Baluarte generates for Compose and Helm."""

import importlib.util
import io
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, insert, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_compliance import body, client, save_profile
from test_compliance import engine as engine

from baluarte.auth.roles import Role
from baluarte.compliance import backups
from baluarte.compliance.retention import expire
from baluarte.db.models import BackupRun, RestoreRun

SHA = "a" * 64
DIGEST_CASES: list[dict[str, Any]] = [
    {},
    {"status": "ok", "checks": {"token": "ok", "a": [1, 2]}},
    {"note": "naïve ✓ unicode", "z": None, "b": True},
    {"n": 1.5, "big": 12345678901234567890, "neg": -0.25, "tiny": 1e-07},
    {"quote": 'say "hi"\\', "ctrl": "tab\tnew\nline", "B": 1, "a": 2, "é": 3},
]
TOOLS = Path(__file__).resolve().parents[2] / "deploy" / "backup" / "backup_tools.py"


def report(engine: Engine, hours_ago: float, **overrides: Any) -> int:
    """Insert a run the way the backup script does: finished_at and reported_by by default."""
    values = {
        "started_at": func.now() - timedelta(hours=hours_ago, minutes=5),
        "status": "succeeded",
        "file_name": "baluarte-20261102T020000Z.dump.age",
        "size_bytes": 1024,
        "sha256": SHA,
        "encrypted": True,
        "recipient": "age1examplepublickey",
        "error": None,
        "host": "backup-1",
    } | overrides
    with engine.begin() as connection:
        run_id = connection.execute(insert(BackupRun).values(**values).returning(BackupRun.id))
        new_id = int(run_id.scalar_one())
        if hours_ago:
            # finished_at defaults to now(); age the row as the database owner would.
            connection.execute(text("ALTER TABLE backup_run DISABLE TRIGGER backup_run_immutable"))
            connection.execute(
                text(
                    "UPDATE backup_run SET finished_at = now() - make_interval(secs => :s) "
                    "WHERE id = :id"
                ),
                {"s": hours_ago * 3600, "id": new_id},
            )
            connection.execute(text("ALTER TABLE backup_run ENABLE TRIGGER backup_run_immutable"))
    return new_id


def restored(engine: Engine, days_ago: float, **overrides: Any) -> int:
    """Insert a restore run the way the backup script does, finished ``days_ago``."""
    values = {
        "started_at": func.now() - timedelta(days=days_ago, minutes=3),
        "status": "succeeded",
        "backup_run_id": 1,
        "file_name": "baluarte-20261102T020000Z.dump.age",
        "sha256": SHA,
        "source": "offsite",
        "schema_revision": "0043",
        "table_count": 66,
        "audit_events": 120,
        "digest_mismatches": 0,
        "error": None,
        "host": "backup-1",
    } | overrides
    with engine.begin() as connection:
        new_id = int(
            connection.execute(
                insert(RestoreRun).values(**values).returning(RestoreRun.id)
            ).scalar_one()
        )
        if days_ago:
            connection.execute(
                text("ALTER TABLE restore_run DISABLE TRIGGER restore_run_immutable")
            )
            connection.execute(
                text(
                    "UPDATE restore_run SET finished_at = now() - make_interval(days => :d) "
                    "WHERE id = :id"
                ),
                {"d": days_ago, "id": new_id},
            )
            connection.execute(text("ALTER TABLE restore_run ENABLE TRIGGER restore_run_immutable"))
    return new_id


def attest(engine: Engine, control_id: str) -> None:
    response = client(engine, 1, Role.ADMIN).post(
        "/api/v1/compliance/attestations",
        json={"control_id": control_id, "statement": "Done another way, recorded elsewhere"},
    )
    assert response.status_code == 201, response.text


def control(engine: Engine, control_id: str) -> dict[str, Any]:
    controls = client(engine, 2, Role.AUDITOR).get("/api/v1/compliance/posture").json()["controls"]
    return next(c for c in controls if c["id"] == control_id)


@pytest.mark.db
def test_a_run_takes_its_defaults_and_cannot_be_changed(engine: Engine) -> None:
    run_id = report(engine, 0)

    with Session(engine) as db:
        run = db.get(BackupRun, run_id)
        assert run is not None
        assert run.finished_at >= run.started_at
        assert run.reported_by  # current_user
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(text("UPDATE backup_run SET size_bytes = 1"))
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM backup_run"))


@pytest.mark.db
@pytest.mark.parametrize(
    "overrides",
    [
        {"encrypted": False},
        {"sha256": None},
        {"file_name": None},
        {"sha256": "not-a-digest"},
        {"status": "partial"},
    ],
)
def test_a_successful_run_must_be_complete_and_encrypted(
    engine: Engine, overrides: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        report(engine, 0, **overrides)


@pytest.mark.db
def test_a_failed_run_is_recorded_with_its_error(engine: Engine) -> None:
    report(
        engine,
        0,
        status="failed",
        file_name=None,
        size_bytes=None,
        sha256=None,
        encrypted=False,
        error="pg_dump: connection refused",
    )

    response = client(engine, 2, Role.AUDITOR).get("/api/v1/compliance/backups")

    assert response.status_code == 200
    assert response.json()["last_success"] is None
    assert response.json()["runs"][0]["error"] == "pg_dump: connection refused"


@pytest.mark.db
def test_off_site_copies_are_recorded_with_the_run(engine: Engine) -> None:
    where = "s3://baluarte-backups/baluarte/baluarte-20261102T020000Z.dump.age (at offsite:8333)"
    report(engine, 2)
    report(engine, 1, offsite_status="failed", offsite_location=where, offsite_error="403")
    report(engine, 0, offsite_status="copied", offsite_location=where)

    response = client(engine, 2, Role.AUDITOR).get("/api/v1/compliance/backups")

    runs = response.json()["runs"]
    assert [(r["offsite_status"], r["offsite_error"]) for r in runs] == [
        ("copied", None),
        ("failed", "403"),
        (None, None),
    ]
    assert response.json()["last_success"]["offsite_location"] == where


@pytest.mark.db
@pytest.mark.parametrize(
    "overrides",
    [
        {"offsite_status": "partial", "offsite_location": "s3://b/k"},
        {"offsite_status": "copied"},
        {"offsite_status": "copied", "offsite_location": "s3://b/k", "offsite_error": "x"},
        {"offsite_status": "failed", "offsite_location": "s3://b/k"},
    ],
)
def test_an_off_site_outcome_must_be_complete(engine: Engine, overrides: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        report(engine, 0, **overrides)


@pytest.mark.db
def test_backup_offsite_follows_the_last_backups_copy(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    where = "s3://baluarte-backups/baluarte/x.dump.age (at offsite:8333)"

    report(engine, 3)
    assert control(engine, "backup-offsite")["status"] == "not_met"
    attest(engine, "backup-offsite")
    # No off-site target: the attestation stands in.
    assert control(engine, "backup-offsite")["status"] == "met"

    report(engine, 2, offsite_status="copied", offsite_location=where)
    met = control(engine, "backup-offsite")
    assert met["status"] == "met"
    assert where in met["detail"]
    assert met["evidence"]["offsite_status"] == "copied"

    # A configured target decides: a failed copy is not met, attestation or not.
    report(engine, 1, offsite_status="failed", offsite_location=where, offsite_error="403")
    failed = control(engine, "backup-offsite")
    assert failed["status"] == "not_met"
    assert "403" in failed["detail"]


@pytest.mark.db
def test_restore_test_follows_the_latest_restore_run(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    assert control(engine, "restore-test")["status"] == "not_met"

    # Older than a year: does not count.
    restored(engine, 400)
    assert control(engine, "restore-test")["status"] == "not_met"

    restored(engine, 30)
    met = control(engine, "restore-test")
    assert met["status"] == "met"
    assert "120 audit events" in met["detail"]

    restored(
        engine,
        1,
        status="failed",
        source=None,
        schema_revision=None,
        table_count=None,
        audit_events=None,
        digest_mismatches=None,
        error="baluarte-x.dump.age from offsite has SHA-256 b, but a was recorded",
    )
    failed = control(engine, "restore-test")
    assert failed["status"] == "not_met"
    assert "was recorded" in failed["detail"]

    # An attestation does not cover a recorded failure.
    attest(engine, "restore-test")
    assert control(engine, "restore-test")["status"] == "not_met"
    restored(engine, 0)
    assert control(engine, "restore-test")["status"] == "met"


@pytest.mark.db
def test_restore_runs_are_listed_and_cannot_be_changed(engine: Engine) -> None:
    restored(engine, 2)
    restored(
        engine,
        0,
        status="failed",
        error="no age private key",
        file_name=None,
        sha256=None,
        source=None,
        schema_revision=None,
        table_count=None,
        audit_events=None,
        digest_mismatches=None,
    )

    response = client(engine, 2, Role.AUDITOR).get("/api/v1/compliance/backups")

    restores = response.json()["restores"]
    assert [r["status"] for r in restores] == ["failed", "succeeded"]
    assert restores[1]["source"] == "offsite"
    assert restores[1]["digest_mismatches"] == 0
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(text("UPDATE restore_run SET status = 'succeeded'"))


@pytest.mark.db
@pytest.mark.parametrize(
    "overrides",
    [
        {"digest_mismatches": 1},
        {"table_count": 0},
        {"schema_revision": None},
        {"source": "tape"},
        {"error": "but it succeeded"},
        {"status": "failed", "error": None},
    ],
)
def test_a_restore_run_must_be_consistent(engine: Engine, overrides: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        restored(engine, 0, **overrides)


def _tools() -> Any:
    spec = importlib.util.spec_from_file_location("backup_tools", TOOLS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("details", DIGEST_CASES)
def test_the_backup_images_digest_matches_the_applications(details: dict[str, Any]) -> None:
    from baluarte.audit import details_digest

    # As PostgreSQL's jsonb prints it, with spaces; the tool reads it back as the app does.
    as_jsonb_text = json.dumps(details, ensure_ascii=False)
    assert _tools().digest(as_jsonb_text) == details_digest(details)


@pytest.mark.db
@pytest.mark.parametrize("details", DIGEST_CASES)
def test_the_digest_matches_through_postgresql_jsonb(
    engine: Engine, details: dict[str, Any]
) -> None:
    from baluarte.audit import details_digest

    with engine.connect() as connection:
        printed = connection.execute(
            text("SELECT CAST(:j AS jsonb)::text"), {"j": json.dumps(details)}
        ).scalar_one()
    assert _tools().digest(printed) == details_digest(details)


def test_the_digest_tool_counts_mismatches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from baluarte.audit import details_digest

    good = {"status": "ok"}
    lines = [
        f"1\t{details_digest(good)}\t{json.dumps(good)}",
        f"2\t{details_digest(good)}\t{json.dumps({'status': 'failed'})}",
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))

    assert _tools().main(["digests"]) == 0
    assert capsys.readouterr().out.split() == ["2", "1", "2"]


def test_list_compares_records_with_stored_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = tmp_path / "records"
    records.write_text(
        f"baluarte-b.dump.age|10|{SHA}|2026-11-02 02:00:00|copied\n"
        f"baluarte-a.dump.age|20|{SHA}|2026-11-01 02:00:00|copied\n"
    )
    local = tmp_path / "local"
    local.write_text("baluarte-b.dump.age|10\nbaluarte-a.dump.age|21\nbaluarte-c.dump.age|5\n")
    remote = tmp_path / "remote"
    remote.write_text(
        json.dumps([{"Name": "baluarte-b.dump.age", "Size": 10, "Metadata": {"sha256": SHA}}])
    )

    assert _tools().main(["list", str(records), str(local), str(remote)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split()[-2:] == ["ok", "ok"]
    assert lines[2].split()[-2:] == ["DIFFERS", "missing"]
    assert "baluarte-c.dump.age" in lines[3] and "(no record)" in lines[3]


@pytest.mark.db
def test_backups_posture_follows_the_interval(engine: Engine) -> None:
    save_profile(engine, ["soc2"])

    assert control(engine, "backups")["status"] == "not_met"

    report(engine, 25)  # within 24 h + 2 h of grace
    met = control(engine, "backups")
    assert met["status"] == "met"
    assert "baluarte-20261102T020000Z.dump.age" in met["detail"]

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM backup_run"))
    report(engine, 27)
    late = control(engine, "backups")
    assert late["status"] == "not_met"
    assert "24-hour interval" in late["detail"]

    # A failed run since does not count.
    report(engine, 0, status="failed", file_name=None, sha256=None, encrypted=False)
    assert control(engine, "backups")["status"] == "not_met"


@pytest.mark.db
def test_the_backup_interval_is_a_profile_setting(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)

    refused = admin.put("/api/v1/compliance/profile", json=body(["soc2"], backup_interval_hours=48))
    out_of_range = admin.put("/api/v1/compliance/profile", json=body([], backup_interval_hours=200))
    allowed = admin.put("/api/v1/compliance/profile", json=body([], backup_interval_hours=48))

    assert refused.status_code == 409
    assert refused.json()["detail"]["violations"][0]["control_id"] == "backup-frequency"
    assert out_of_range.status_code == 422
    assert allowed.status_code == 200
    assert allowed.json()["settings"]["backup_interval_hours"] == 48
    # A client that does not know the setting keeps its value.
    legacy = body([])
    del legacy["settings"]["backup_interval_hours"]
    kept = admin.put("/api/v1/compliance/profile", json=legacy)
    assert kept.status_code == 200, kept.text
    assert kept.json()["settings"]["backup_interval_hours"] == 48


@pytest.mark.db
def test_backup_controls_are_attestable_where_they_take_an_attestation(engine: Engine) -> None:
    save_profile(engine, ["iso27001"])
    admin = client(engine, 1, Role.ADMIN)

    refused = admin.post(
        "/api/v1/compliance/attestations",
        json={"control_id": "backups", "statement": "We take backups every night"},
    )
    restore = admin.post(
        "/api/v1/compliance/attestations",
        json={
            "control_id": "restore-test",
            "statement": "Restored the 2026-10-01 backup into a scratch database; digests match",
        },
    )

    assert refused.status_code == 422
    assert restore.status_code == 201
    assert control(engine, "restore-test")["status"] == "met"
    assert control(engine, "backup-offsite")["status"] == "not_met"
    catalogue = client(engine, 2, Role.AUDITOR).get("/api/v1/compliance/catalogue").json()
    days = {c["id"]: c["attestation_days"] for c in catalogue["controls"]}
    assert days["restore-test"] == 365
    assert days["backup-offsite"] == 90
    assert days["log-shipping"] == 90
    assert days["backups"] is None


@pytest.mark.db
def test_services_describe_how_to_run_the_backup(engine: Engine) -> None:
    admin = client(engine, 1, Role.ADMIN)
    assert (
        admin.put(
            "/api/v1/compliance/profile", json=body(["soc2"], backup_interval_hours=12)
        ).status_code
        == 200
    )

    response = client(engine, 2, Role.AUDITOR).get("/api/v1/compliance/services")

    assert response.status_code == 200
    by_id = {s["id"]: s for s in response.json()}
    backup = by_id["backup"]
    assert backup["required"] is True
    assert backup["detected"] is False and backup["healthy"] is False
    assert "docker compose --profile backup up -d" in backup["how_to"]["compose"]
    assert "age-keygen -o baluarte-backup.key" in backup["how_to"]["compose"]
    assert "secrets/backup_database_password" in backup["how_to"]["compose"]
    assert "mkdir -p backups && sudo chown 70:70 backups" in backup["how_to"]["compose"]
    assert "--set backup.enabled=true" in backup["how_to"]["helm"]
    assert "--set backup.recipient=age1..." in backup["how_to"]["helm"]
    compose = backup["config"]["compose"]
    assert compose["file_name"] == "baluarte-backup.env"
    assert "BALUARTE_BACKUP_INTERVAL_HOURS=12\n" in compose["content"]
    assert "BALUARTE_BACKUP_KEEP=14\n" in compose["content"]
    assert "BALUARTE_BACKUP_RECIPIENT=" in compose["content"]
    helm = backup["config"]["helm"]
    assert helm["file_name"] == "backup-values.yaml"
    assert 'schedule: "17 */12 * * *"' in helm["content"]
    assert "enabled: true" in helm["content"] and "keep: 14" in helm["content"]

    shipping_service = by_id["audit-shipping"]
    assert shipping_service["config"] is None
    assert shipping_service["required"] is True
    assert "Settings > Audit log shipping" in shipping_service["how_to"]["compose"]

    report(engine, 1)
    backup = {s["id"]: s for s in admin.get("/api/v1/compliance/services").json()}["backup"]
    assert backup["detected"] is True and backup["healthy"] is True


def test_the_schedule_runs_at_least_as_often_as_the_interval() -> None:
    assert backups.schedule(1) == "17 */1 * * *"
    assert backups.schedule(6) == "17 */6 * * *"
    assert backups.schedule(24) == "17 2 * * *"
    assert backups.schedule(36) == "17 2 * * *"
    assert backups.schedule(72) == "17 2 */3 * *"


@pytest.mark.db
def test_retention_keeps_the_latest_successful_backup_run(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    latest = report(engine, 24 * 400)
    older = report(engine, 24 * 401)
    failed = report(engine, 24 * 399, status="failed", file_name=None, sha256=None, encrypted=False)

    with Session(engine) as db:
        details = expire(db)
        db.commit()
        assert details["deleted"]["backup_run"] == 2
        assert db.get(BackupRun, latest) is not None
        assert db.get(BackupRun, older) is None
        assert db.get(BackupRun, failed) is None


@pytest.mark.db
def test_retention_keeps_the_latest_successful_restore_run(engine: Engine) -> None:
    save_profile(engine, ["soc2"])
    latest = restored(engine, 400)
    older = restored(engine, 401)

    with Session(engine) as db:
        details = expire(db)
        db.commit()
        assert details["deleted"]["restore_run"] == 1
        assert db.get(RestoreRun, latest) is not None
        assert db.get(RestoreRun, older) is None
