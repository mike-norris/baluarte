"""Built-in TOTP enrollments, for administrators: who has an authenticator, and resetting a lost
one (docs/SECURITY.md#built-in-totp)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth import mfa
from baluarte.auth.dependencies import Principal, database, require_deployment
from baluarte.auth.roles import Permission

router = APIRouter(prefix="/settings/mfa", tags=["settings"])

Admin = Annotated[Principal, Depends(require_deployment(Permission.SETTINGS_MANAGE))]
Database = Annotated[Session, Depends(database)]


class EnrollmentOut(BaseModel):
    user_id: int
    email: str | None
    display_name: str | None
    confirmed_at: datetime
    recovery_codes_left: int


@router.get(
    "/enrollments",
    response_model=list[EnrollmentOut],
    summary="Users with a built-in TOTP authenticator",
)
def list_enrollments(principal: Admin, db: Database) -> list[EnrollmentOut]:
    return [EnrollmentOut(**vars(row)) for row in mfa.enrolled_users(db)]


@router.delete(
    "/enrollments/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset a user's authenticator",
    description=(
        "Removes the authenticator and recovery codes and ends the user's live sessions; their "
        "next sign-in sets up a new one. Not one's own: another administrator resets it."
    ),
)
def reset_enrollment(user_id: int, principal: Admin, db: Database) -> Response:
    if user_id == principal.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Another administrator must reset your authenticator"
        )
    ended = mfa.reset(db, user_id)
    if ended is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That user has no authenticator")
    audit.record(
        db,
        actor=principal.actor,
        action="settings.mfa_reset",
        resource_type="user",
        resource_id=str(user_id),
        details={"sessions_ended": ended},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
