"""Audit log shipping settings (``settings:manage``). See docs/COMPLIANCE.md.

Credentials are sealed with the secret key and never returned; a GET reports each as
``configured`` or ``not_set``. Shipping progress is under ``GET /compliance/audit-shipping``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from baluarte.auth.dependencies import Principal, database, require
from baluarte.auth.roles import Permission
from baluarte.compliance import shipping
from baluarte.compliance.schemas import (
    AuditShippingConfig,
    AuditShippingTestResult,
    AuditShippingUpdate,
)

router = APIRouter(prefix="/settings/audit-shipping", tags=["settings"])

Admin = Annotated[Principal, Depends(require(Permission.SETTINGS_MANAGE))]
Database = Annotated[Session, Depends(database)]


def audit_shipping_sender() -> shipping.SenderFactory:
    """How the connection test reaches the destination. Overridden in tests."""
    return shipping.default_sender


@contextmanager
def shipping_errors() -> Iterator[None]:
    try:
        yield
    except shipping.ShippingInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except shipping.ShippingConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _settings(body: AuditShippingUpdate) -> dict[str, Any]:
    return body.model_dump(exclude={"api_key", "password"})


@router.get("", response_model=AuditShippingConfig, summary="Audit log shipping settings")
def read_audit_shipping(_: Admin, db: Database) -> AuditShippingConfig:
    return AuditShippingConfig.model_validate(shipping.settings_view(db))


@router.put(
    "",
    response_model=AuditShippingConfig,
    summary="Change the audit log shipping settings",
    description="A null `api_key` or `password` keeps the stored one, which must be entered "
    "again when the URL changes. 409 when no secret key is configured to seal a credential. "
    "Audited as `settings.audit_shipping_updated`.",
)
def write_audit_shipping(
    body: AuditShippingUpdate, principal: Admin, db: Database
) -> AuditShippingConfig:
    with shipping_errors():
        shipping.update(
            db,
            _settings(body),
            api_key=body.api_key,
            password=body.password,
            actor=principal.actor,
        )
    db.commit()
    return AuditShippingConfig.model_validate(shipping.settings_view(db))


@router.post(
    "/test",
    response_model=AuditShippingTestResult,
    summary="Send a connection test message",
    description="Sends one `connection_test` message to the destination in the body, without "
    "saving it. TLS is always verified.",
)
def test_audit_shipping(
    body: AuditShippingUpdate,
    _: Admin,
    db: Database,
    sender: Annotated[shipping.SenderFactory, Depends(audit_shipping_sender)],
) -> AuditShippingTestResult:
    with shipping_errors():
        ok, detail = shipping.test_destination(
            db,
            _settings(body),
            api_key=body.api_key,
            password=body.password,
            sender_factory=sender,
        )
    return AuditShippingTestResult(ok=ok, detail=detail)
