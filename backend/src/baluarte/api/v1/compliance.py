"""The operating profile Baluarte itself runs to, and its posture against it.

Reading needs ``compliance:read`` (admin and auditor). Changing the profile, requesting and
deciding exceptions and attesting need ``settings:manage``; an exception is approved or rejected
by someone other than its requester. Every change is audited. See docs/COMPLIANCE.md.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.compliance import (
    attestations,
    backups,
    exceptions,
    operations,
    posture,
    services,
    shipping,
)
from baluarte.compliance.catalogue import CONTROLS, FRAMEWORKS
from baluarte.compliance.exceptions import Actor
from baluarte.compliance.schemas import (
    AttestationIn,
    AttestationOut,
    AttestationRef,
    AuditShippingStatus,
    BackupRunOut,
    BackupsOut,
    Catalogue,
    ComplianceExceptionIn,
    ComplianceExceptionOut,
    ControlOut,
    ControlPostureOut,
    DecisionIn,
    ExceptionRef,
    FrameworkOut,
    MinimumOut,
    OperatingSettings,
    PostureOut,
    PostureSummary,
    ProfileOut,
    ProfileUpdate,
    RequirementOut,
    ServiceOut,
)
from baluarte.configuration import effective
from baluarte.db.models import BackupRun, ComplianceAttestation, ComplianceException

router = APIRouter(prefix="/compliance", tags=["compliance"])

Reader = Annotated[Principal, Depends(require(Permission.COMPLIANCE_READ))]
Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]
Database = Annotated[Session, Depends(database)]
ExceptionId = Annotated[int, Path(gt=0)]


def _conflict(code: str, message: str, **extra: object) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, detail={"code": code, "message": message, **extra}
    )


@contextmanager
def compliance_errors() -> Iterator[None]:
    """Map the profile's, exceptions' and attestations' errors to HTTP answers."""
    try:
        yield
    except operations.BelowMinimum as exc:
        raise _conflict(
            "below_minimum", str(exc), violations=[v.as_dict() for v in exc.violations]
        ) from exc
    except operations.DeploymentSetting as exc:
        raise _conflict("deployment_setting", str(exc), settings=exc.settings) from exc
    except exceptions.NotAllowed as exc:
        raise _conflict("not_allowed", str(exc)) from exc
    except exceptions.ExceptionConflict as exc:
        raise _conflict("conflict", str(exc)) from exc
    except exceptions.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (
        operations.ProfileInvalid,
        exceptions.ExceptionInvalid,
        attestations.AttestationInvalid,
    ) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def profile_out(profile: operations.Profile) -> ProfileOut:
    return ProfileOut(
        frameworks=list(profile.frameworks),
        configured=profile.configured,
        settings=OperatingSettings.model_validate(profile.settings),
        sources=profile.sources,
        minimums={
            name: MinimumOut(op=m.op, value=m.value, frameworks=list(m.frameworks))
            for name, m in profile.minimums().items()
        },
        updated_by=profile.updated_by,
        updated_at=profile.updated_at,
    )


def update_profile(db: Session, body: ProfileUpdate, actor: str) -> ProfileOut:
    """Shared by setup and settings: validate, store, audit and commit."""
    with compliance_errors():
        profile = operations.update(db, list(body.frameworks), body.settings, actor)
    db.commit()
    effective.invalidate()
    return profile_out(profile)


def _actor(principal: Principal) -> Actor:
    return Actor(user_id=principal.user_id, actor=principal.actor)


def _exception_out(row: ComplianceException) -> ComplianceExceptionOut:
    return ComplianceExceptionOut(
        id=row.id,
        control_id=row.control_id,
        justification=row.justification,
        expires_on=row.expires_on,
        state=row.state,  # type: ignore[arg-type]
        in_force=exceptions.in_force(row),
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        self_approved=row.self_approved,
        note=row.decision_note,
    )


def _attestation_out(row: ComplianceAttestation) -> AttestationOut:
    return AttestationOut(
        id=row.id,
        control_id=row.control_id,
        statement=row.statement,
        attested_by=row.attested_by,
        attested_at=row.attested_at,
        expires_at=row.expires_at,
    )


def posture_out(result: posture.Posture) -> PostureOut:
    return PostureOut(
        evaluated_at=result.evaluated_at,
        frameworks=list(result.frameworks),
        configured=result.configured,
        summary=PostureSummary(**result.summary()),
        controls=[
            ControlPostureOut(
                id=item.control.id,
                title=item.control.title,
                status=item.status,
                detail=item.detail,
                evidence=item.evidence,
                exception=ExceptionRef(**item.exception) if item.exception else None,
                attestation=(
                    AttestationRef(
                        id=item.attestation.id,
                        attested_by=item.attestation.attested_by,
                        attested_at=item.attestation.attested_at,
                        expires_at=item.attestation.expires_at,
                    )
                    if item.attestation is not None
                    else None
                ),
            )
            for item in result.controls
        ],
    )


@router.get("/catalogue", response_model=Catalogue, summary="Frameworks and controls")
def read_catalogue(_: Reader) -> Catalogue:
    return Catalogue(
        frameworks=[FrameworkOut(id=f.id, name=f.name) for f in FRAMEWORKS],
        controls=[
            ControlOut(
                id=c.id,
                title=c.title,
                area=c.area,
                kind=c.kind,
                description=c.description,
                how_to=c.how_to,
                mappings={framework: list(refs) for framework, refs in c.mappings.items()},
                setting=c.setting,
                requirements=(
                    {f: RequirementOut(op=r.op, value=r.value) for f, r in reqs.items()}
                    if (reqs := c.requirements()) is not None
                    else None
                ),
                attestation_days=c.attestation_days,
            )
            for c in CONTROLS
        ],
    )


@router.get("/profile", response_model=ProfileOut, summary="The operating profile in force")
def read_profile(_: Reader, db: Database) -> ProfileOut:
    return profile_out(operations.load(db))


@router.put(
    "/profile",
    response_model=ProfileOut,
    summary="Change the operating profile",
    description="409 `below_minimum` when a value misses the selected frameworks' minimum and no "
    "exception is in force for its control; 409 `deployment_setting` when the request changes a "
    "setting the environment fixes. Audited as `settings.compliance_updated`.",
)
def write_profile(body: ProfileUpdate, principal: Admin, db: Database) -> ProfileOut:
    return update_profile(db, body, principal.actor)


@router.get("/posture", response_model=PostureOut, summary="Posture against the profile")
def read_posture(_: Reader, db: Database) -> PostureOut:
    return posture_out(posture.evaluate(db))


@router.get(
    "/audit-shipping",
    response_model=AuditShippingStatus,
    summary="How far audit shipping has got",
    description="`lag_events` counts the events the destination has not received; 0 while "
    "shipping is off.",
)
def read_audit_shipping_status(_: Reader, db: Database) -> AuditShippingStatus:
    return AuditShippingStatus.model_validate(shipping.status(db), from_attributes=True)


def _backup_run_out(row: BackupRun) -> BackupRunOut:
    return BackupRunOut.model_validate(row, from_attributes=True)


@router.get(
    "/backups",
    response_model=BackupsOut,
    summary="Backup runs the backup service reported, newest first",
)
def read_backups(_: Reader, db: Database) -> BackupsOut:
    overview = backups.overview(db)
    return BackupsOut(
        interval_hours=overview.interval_hours,
        last_success=(
            _backup_run_out(overview.last_success) if overview.last_success is not None else None
        ),
        runs=[_backup_run_out(row) for row in overview.runs],
    )


@router.get(
    "/services",
    response_model=list[ServiceOut],
    summary="Services the profile needs, their state and how to run them",
    description="Baluarte never starts a container; each service carries Compose and Helm "
    "steps and, where it has one, a generated configuration file.",
)
def read_services(_: Reader, db: Database) -> list[ServiceOut]:
    result = posture.evaluate(db)
    return [ServiceOut.model_validate(s.as_dict()) for s in services.services(db, result)]


@router.get(
    "/exceptions",
    response_model=list[ComplianceExceptionOut],
    summary="Compliance exceptions, newest first",
)
def list_exceptions(_: Reader, db: Database) -> list[ComplianceExceptionOut]:
    return [_exception_out(row) for row in exceptions.listing(db)]


@router.post(
    "/exceptions",
    response_model=ComplianceExceptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request a compliance exception",
    description="`proposed` while two-person review is required, else approved at once and "
    "recorded `self_approved`.",
)
def request_exception(
    body: ComplianceExceptionIn, principal: Admin, db: Database
) -> ComplianceExceptionOut:
    with compliance_errors():
        row = exceptions.request(
            db, body.control_id, body.justification, body.expires_on, _actor(principal)
        )
    db.commit()
    effective.invalidate()
    return _exception_out(row)


@router.post(
    "/exceptions/{exception_id}/approve",
    response_model=ComplianceExceptionOut,
    summary="Approve a proposed compliance exception",
    description="409 `not_allowed` for the requester's own exception.",
)
def approve_exception(
    exception_id: ExceptionId, principal: Admin, db: Database, body: DecisionIn | None = None
) -> ComplianceExceptionOut:
    with compliance_errors():
        row = exceptions.approve(
            db, exception_id, _actor(principal), body.note if body is not None else None
        )
    db.commit()
    return _exception_out(row)


@router.post(
    "/exceptions/{exception_id}/reject",
    response_model=ComplianceExceptionOut,
    summary="Reject a proposed compliance exception",
    description="Needs a note. 409 `not_allowed` for the requester's own exception.",
)
def reject_exception(
    exception_id: ExceptionId, principal: Admin, db: Database, body: DecisionIn | None = None
) -> ComplianceExceptionOut:
    with compliance_errors():
        row = exceptions.reject(
            db, exception_id, _actor(principal), (body.note if body is not None else None) or ""
        )
    db.commit()
    return _exception_out(row)


@router.post(
    "/exceptions/{exception_id}/revoke",
    response_model=ComplianceExceptionOut,
    summary="Revoke a proposed or approved compliance exception",
)
def revoke_exception(
    exception_id: ExceptionId, principal: Admin, db: Database
) -> ComplianceExceptionOut:
    with compliance_errors():
        row = exceptions.revoke(db, exception_id, _actor(principal))
    db.commit()
    return _exception_out(row)


@router.get(
    "/attestations",
    response_model=list[AttestationOut],
    summary="Attestations, newest first",
)
def list_attestations(
    _: Reader,
    db: Database,
    control_id: Annotated[str | None, Query(max_length=64)] = None,
) -> list[AttestationOut]:
    return [_attestation_out(row) for row in attestations.listing(db, control_id)]


@router.post(
    "/attestations",
    response_model=AttestationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Attest that a control is in place",
    description="Any control with `attestation_days` takes one. Audited as `compliance.attested`.",
)
def attest(body: AttestationIn, principal: Admin, db: Database) -> AttestationOut:
    with compliance_errors():
        row = attestations.record(db, body.control_id, body.statement, _actor(principal))
    db.commit()
    return _attestation_out(row)
