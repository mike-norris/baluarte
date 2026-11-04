"""Baluarte's posture against its operating profile: one result per control.

A control's own result is ``met``, ``not_met`` or ``needs_attestation``. Then:

* with no framework selected every control is ``not_applicable``; its own result stays in
  ``evidence["result"]`` for information, and nothing is enforced;
* a control that is not met but has an exception in force is ``excepted``.

A check that cannot run (the database refuses a query, a setting cannot be read) is ``not_met``
and says why; it is never reported as met.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from baluarte.audit import details_digest
from baluarte.compliance import attestations, operations
from baluarte.compliance.catalogue import CONTROLS, Control, FrameworkId
from baluarte.compliance.exceptions import controls_in_force
from baluarte.config import database_url
from baluarte.db.models import AuditEvent, CollectionJob, ComplianceAttestation
from baluarte.settings import ConfigurationError, development_mode

logger = logging.getLogger(__name__)

Status = Literal["met", "not_met", "needs_attestation", "excepted", "not_applicable"]
STATUSES: tuple[Status, ...] = ("met", "not_met", "needs_attestation", "excepted", "not_applicable")
ENCRYPTED_SSL_MODES = ("require", "verify-ca", "verify-full")
RETENTION_WITHIN = timedelta(hours=48)
# Audit events may wait this long for shipping, and the last delivery be this old.
SHIPPING_WITHIN = timedelta(minutes=15)
# Verifying every audit digest reads the whole table, so a result is reused for a while.
AUDIT_CHECK_SECONDS = 300.0


@dataclass
class Result:
    status: Literal["met", "not_met", "needs_attestation"]
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlPosture:
    control: Control
    status: Status
    detail: str
    evidence: dict[str, Any]
    exception: dict[str, Any] | None
    attestation: ComplianceAttestation | None


@dataclass(frozen=True)
class Posture:
    evaluated_at: datetime
    frameworks: tuple[FrameworkId, ...]
    configured: bool
    controls: list[ControlPosture]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
        for control in self.controls:
            counts[control.status] += 1
        return counts


@dataclass
class Context:
    """What the checks look at, gathered once. Tests replace the callables."""

    public_url: Callable[[], str | None]
    gitlab_url: Callable[[], str | None]
    database_ssl_mode: Callable[[], str | None]
    secret_key_configured: Callable[[], bool]
    freshness: Callable[[Session], Any]


def _public_url() -> str | None:
    # Imported here: effective settings import the profile for session limits.
    from baluarte.configuration.effective import effective_auth_settings

    settings = effective_auth_settings()
    return settings.public_url if settings is not None else None


def _gitlab_url() -> str | None:
    from baluarte.integrations.gitlab.settings import load_gitlab_settings

    settings = load_gitlab_settings()
    return settings.url if settings is not None else None


def _ssl_mode() -> str | None:
    mode = database_url().query.get("sslmode")
    return mode if isinstance(mode, str) else None


def _secret_key() -> bool:
    from baluarte.configuration.crypto import load_secret_box

    try:
        return load_secret_box() is not None
    except ConfigurationError:
        return False


def _freshness(db: Session) -> Any:
    from baluarte.integrations.gitlab.freshness import report
    from baluarte.integrations.gitlab.instances import instance_id_for
    from baluarte.integrations.gitlab.settings import load_gitlab_settings

    settings = load_gitlab_settings()
    instance_id = instance_id_for(db, settings.url) if settings is not None else None
    return report(db, instance_id, settings)


def default_context() -> Context:
    return Context(_public_url, _gitlab_url, _ssl_mode, _secret_key, _freshness)


# Settings ------------------------------------------------------------------------------------


def _setting(control: Control, profile: operations.Profile) -> Result:
    setting = control.setting or ""
    value = profile.settings[setting]
    source = profile.sources[setting]
    required = profile.minimums().get(setting)
    evidence: dict[str, Any] = {"setting": setting, "value": value, "source": source}
    if required is None:
        return Result("met", f"{setting} is {value}; no framework sets a minimum.", evidence)
    rule = f"{required.op} {required.value}"
    evidence["required"] = {"op": required.op, "value": required.value}
    evidence["frameworks"] = list(required.frameworks)
    if required.met_by(value):
        return Result("met", f"{setting} is {value} (required {rule}).", evidence)
    where = " The deployment's environment sets it." if source == "deployment" else ""
    if setting == operations.REVIEW and development_mode():
        where += " Development mode allows this without an exception."
    return Result("not_met", f"{setting} is {value}; the profile requires {rule}.{where}", evidence)


# Checks --------------------------------------------------------------------------------------

_audit_lock = threading.Lock()
_audit_cache: dict[tuple[str, int, int], tuple[float, Result]] = {}


def reset_audit_cache() -> None:
    with _audit_lock:
        _audit_cache.clear()


def _audit_integrity(db: Session, _context: Context) -> Result:
    count, last_id = db.execute(
        select(func.count(), func.coalesce(func.max(AuditEvent.id), 0))
    ).one()
    database = str(db.execute(text("SELECT current_database()")).scalar_one())
    key = (database, int(count), int(last_id))
    now = time.monotonic()
    with _audit_lock:
        hit = _audit_cache.get(key)
        if hit is not None and now - hit[0] < AUDIT_CHECK_SECONDS:
            return hit[1]
    invalid: list[int] = []
    for event_id, details, digest in db.execute(
        select(AuditEvent.id, AuditEvent.details, AuditEvent.details_digest)
        .where(AuditEvent.id <= last_id)
        .order_by(AuditEvent.id)
        .execution_options(yield_per=1000)
    ):
        if details_digest(details) != digest:
            invalid.append(event_id)
    evidence = {
        "events": count,
        "last_id": last_id,
        "invalid": len(invalid),
        "invalid_ids": invalid[:20],
    }
    if invalid:
        result = Result(
            "not_met",
            f"{len(invalid)} of {count} audit events no longer match their digest.",
            evidence,
        )
    else:
        result = Result("met", f"All {count} audit events match their digest.", evidence)
    with _audit_lock:
        _audit_cache[key] = (now, result)
    return result


def _audit_protection(db: Session, _context: Context) -> Result:
    row = db.execute(
        text(
            """
            SELECT current_user AS role,
                   r.rolsuper AS superuser,
                   pg_has_role(current_user, c.relowner, 'MEMBER') AS owner,
                   has_table_privilege('audit_event', 'UPDATE') AS can_update,
                   has_table_privilege('audit_event', 'DELETE') AS can_delete,
                   has_table_privilege('audit_event', 'TRUNCATE') AS can_truncate
              FROM pg_roles r, pg_class c
             WHERE r.rolname = current_user AND c.oid = 'audit_event'::regclass
            """
        )
    ).one()
    evidence = {
        "role": row.role,
        "superuser": row.superuser,
        "owns_audit_table": row.owner,
        "update": row.can_update,
        "delete": row.can_delete,
        "truncate": row.can_truncate,
    }
    problems = [
        what
        for what, present in (
            ("is a superuser", row.superuser),
            ("owns the audit table", row.owner),
            ("may UPDATE audit events", row.can_update),
            ("may DELETE audit events", row.can_delete),
            ("may TRUNCATE the audit table", row.can_truncate),
        )
        if present
    ]
    if problems:
        return Result(
            "not_met",
            f"The application's database role {row.role} " + ", ".join(problems) + ".",
            evidence,
        )
    return Result(
        "met",
        f"The application's database role {row.role} can only read and add audit events.",
        evidence,
    )


def _secrets_sealed(_db: Session, context: Context) -> Result:
    if context.secret_key_configured():
        return Result("met", "A secret key is configured; stored secrets are sealed.", {})
    return Result(
        "not_met",
        "No secret key is configured (BALUARTE_SECRET_KEY_FILE), so no secret can be stored "
        "sealed.",
        {},
    )


def _https(url: str | None, what: str) -> Result:
    if url is None:
        return Result("not_met", f"The {what} is not configured.", {"url": None})
    if url.startswith("https://"):
        return Result("met", f"The {what} is {url}.", {"url": url})
    return Result("not_met", f"The {what} is {url}, not https.", {"url": url})


def _tls_web(_db: Session, context: Context) -> Result:
    return _https(context.public_url(), "public URL")


def _tls_gitlab(_db: Session, context: Context) -> Result:
    return _https(context.gitlab_url(), "GitLab URL")


def _tls_database(db: Session, context: Context) -> Result:
    mode = context.database_ssl_mode()
    encrypted = db.execute(
        text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
    ).scalar_one_or_none()
    evidence = {"sslmode": mode, "connection_encrypted": bool(encrypted)}
    if encrypted and mode in ENCRYPTED_SSL_MODES:
        return Result("met", f"The connection is encrypted and sslmode is {mode}.", evidence)
    if not encrypted:
        return Result("not_met", "The connection to the database is not encrypted.", evidence)
    return Result(
        "not_met",
        f"The connection is encrypted, but sslmode is {mode or 'not set'}, so it could fall back "
        "to plain text.",
        evidence,
    )


def _retention_running(db: Session, _context: Context) -> Result:
    from baluarte.compliance.retention import RETENTION_EXPIRE

    now: datetime = db.execute(select(func.now())).scalar_one()
    last = db.execute(
        select(func.max(CollectionJob.finished_at)).where(
            CollectionJob.kind == RETENTION_EXPIRE, CollectionJob.state == "succeeded"
        )
    ).scalar_one()
    hold = bool(operations.load(db).settings["legal_hold"])
    evidence = {"last_success_at": last.isoformat() if last else None, "legal_hold": hold}
    if last is not None and now - last <= RETENTION_WITHIN:
        held = " Legal hold is on, so it deleted nothing." if hold else ""
        return Result("met", f"Retention last ran at {last.isoformat()}.{held}", evidence)
    seen = f"last succeeded at {last.isoformat()}" if last else "has not succeeded"
    return Result("not_met", f"The retention job {seen}.", evidence)


def _collection_health(db: Session, context: Context) -> Result:
    report = context.freshness(db)
    if report.status == "not_configured":
        return Result("not_met", "GitLab is not configured.", {"status": report.status})
    if report.alert is not None:
        return Result(
            "not_met",
            report.alert.message,
            {"status": report.status, "reason": report.alert.reason},
        )
    return Result("met", "Scheduled GitLab collection is succeeding.", {"status": report.status})


def _inventory_population(db: Session, context: Context) -> Result:
    report = context.freshness(db)
    population = report.population
    if population is None:
        return Result("not_met", "GitLab is not configured.", {"basis": None})
    evidence = {"basis": population.basis, "token_username": population.token_username}
    return Result(
        "met" if population.basis == "instance" else "not_met", population.statement, evidence
    )


def _current_attestation(db: Session, control_id: str) -> ComplianceAttestation | None:
    now: datetime = db.execute(select(func.now())).scalar_one()
    latest = attestations.latest(db).get(control_id)
    return latest if latest is not None and latest.expires_at > now else None


def _log_shipping(db: Session, _context: Context) -> Result:
    from baluarte.compliance import shipping

    now: datetime = db.execute(select(func.now())).scalar_one()
    state = shipping.status(db)
    evidence: dict[str, Any] = {
        "enabled": state.enabled,
        "destination": state.destination,
        "lag_events": state.lag_events,
        "last_shipped_id": state.last_shipped_id,
        "last_success_at": state.last_success_at.isoformat() if state.last_success_at else None,
    }
    if state.enabled:
        target = state.destination or "the destination"
        if state.lag_events == 0:
            return Result("met", f"Every audit event has been shipped to {target}.", evidence)
        oldest = state.oldest_unshipped_at
        succeeded = state.last_success_at
        if (
            oldest is not None
            and now - oldest <= SHIPPING_WITHIN
            and succeeded is not None
            and now - succeeded <= SHIPPING_WITHIN
        ):
            return Result(
                "met",
                f"Shipping to {target}; {state.lag_events} recent events are on their way.",
                evidence,
            )
        since = f" since {oldest.isoformat()}" if oldest is not None else ""
        error = f" Last error: {state.last_error}" if state.last_error else ""
        return Result(
            "not_met",
            f"{state.lag_events} audit events have waited{since} to be shipped to {target}.{error}",
            evidence,
        )
    attested = _current_attestation(db, "log-shipping")
    if attested is not None:
        evidence["attestation_id"] = attested.id
        return Result(
            "met",
            "Audit shipping is off; the platform's log collector ships audit events, as "
            f"{attested.attested_by} attested, current until {attested.expires_at.isoformat()}.",
            evidence,
        )
    return Result(
        "not_met",
        "Audit shipping is off, and nobody has attested that the platform's log collector "
        "ships audit events.",
        evidence,
    )


def _backups(db: Session, _context: Context) -> Result:
    from baluarte.compliance import backups

    now: datetime = db.execute(select(func.now())).scalar_one()
    hours = backups.interval_hours(db)
    last = backups.last_success(db)
    evidence: dict[str, Any] = {"interval_hours": hours}
    if last is None:
        return Result(
            "not_met", "The backup service has not reported an encrypted backup.", evidence
        )
    evidence.update(
        {
            "backup_run_id": last.id,
            "finished_at": last.finished_at.isoformat(),
            "file_name": last.file_name,
            "sha256": last.sha256,
        }
    )
    named = f"{last.file_name} finished at {last.finished_at.isoformat()}"
    if now <= backups.due_by(last, hours):
        return Result("met", f"The last encrypted backup, {named}.", evidence)
    return Result(
        "not_met",
        f"The last encrypted backup, {named}, is older than the {hours}-hour interval plus "
        "2 hours.",
        evidence,
    )


CHECKS: dict[str, Callable[[Session, Context], Result]] = {
    "audit-integrity": _audit_integrity,
    "audit-protection": _audit_protection,
    "secrets-sealed": _secrets_sealed,
    "tls-web": _tls_web,
    "tls-database": _tls_database,
    "tls-gitlab": _tls_gitlab,
    "retention-running": _retention_running,
    "log-shipping": _log_shipping,
    "backups": _backups,
    "collection-health": _collection_health,
    "inventory-population": _inventory_population,
}


def _attestation(control: Control, latest: ComplianceAttestation | None, now: datetime) -> Result:
    if latest is None:
        return Result("needs_attestation", "Nobody has attested to this yet.", {})
    evidence = {"attestation_id": latest.id, "expires_at": latest.expires_at.isoformat()}
    if latest.expires_at <= now:
        return Result(
            "needs_attestation",
            f"The last attestation expired at {latest.expires_at.isoformat()}.",
            evidence,
        )
    return Result(
        "met",
        f"Attested by {latest.attested_by} at {latest.attested_at.isoformat()}, current until "
        f"{latest.expires_at.isoformat()}.",
        evidence,
    )


def _run(db: Session, control: Control, context: Context) -> Result:
    check = CHECKS[control.id]
    try:
        with db.begin_nested():
            return check(db, context)
    except (SQLAlchemyError, ConfigurationError) as exc:
        logger.warning("compliance: check %s could not run (%s)", control.id, type(exc).__name__)
        return Result(
            "not_met", f"The check could not run ({type(exc).__name__}).", {"error": True}
        )


def evaluate(db: Session, context: Context | None = None) -> Posture:
    context = context or default_context()
    profile = operations.load(db)
    now: datetime = db.execute(select(func.now())).scalar_one()
    excepted = controls_in_force(db)
    latest = attestations.latest(db)
    results: list[ControlPosture] = []
    for control in CONTROLS:
        if control.kind == "setting":
            result = _setting(control, profile)
        elif control.kind == "attestation":
            result = _attestation(control, latest.get(control.id), now)
        else:
            result = _run(db, control, context)
        status: Status = result.status
        evidence = dict(result.evidence)
        exception = excepted.get(control.id)
        if not profile.frameworks:
            evidence["result"] = result.status
            status = "not_applicable"
        elif result.status != "met" and exception is not None:
            status = "excepted"
        results.append(
            ControlPosture(
                control=control,
                status=status,
                detail=result.detail,
                evidence=evidence,
                exception=(
                    {"id": exception.id, "expires_on": exception.expires_on}
                    if exception is not None
                    else None
                ),
                attestation=(
                    latest.get(control.id) if control.attestation_days is not None else None
                ),
            )
        )
    return Posture(
        evaluated_at=now,
        frameworks=profile.frameworks,
        configured=profile.configured,
        controls=results,
    )


def operations_summary(db: Session, context: Context | None = None) -> dict[str, Any]:
    """The operating profile and posture as the evidence manifest states them."""
    result = evaluate(db, context)
    profile = operations.load(db)
    return {
        "configured": profile.configured,
        "development_mode": development_mode(),
        "frameworks": list(profile.frameworks),
        "settings": profile.settings,
        "posture": {
            "summary": result.summary(),
            "controls": [{"id": c.control.id, "status": c.status} for c in result.controls],
        },
        "exceptions_in_force": [
            {"id": row.id, "control_id": row.control_id, "expires_on": row.expires_on.isoformat()}
            for row in sorted(controls_in_force(db).values(), key=lambda row: row.id)
        ],
    }
