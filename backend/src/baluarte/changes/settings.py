"""How change evidence is collected, stored in the ``change_evidence`` setting.

- ``lookback_days``: how far back a project's first collection reads (1-730, default 90). A
  project's change history starts there; nothing earlier is ever read.
- ``redeploy_days``: merge requests merged within this many days are read again each collection,
  so a deployment that comes later is recorded (1-30, default 7).
- ``production_environments``: the environments whose successful deployments are recorded
  (1-20 names, default ``["production"]``).

Changing them is audited as ``settings.change_evidence_updated``. A shorter look-back does not
remove history already collected.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.configuration.store import read_setting, write_setting

CHANGE_EVIDENCE = "change_evidence"

EnvironmentName = Annotated[str, Field(min_length=1, max_length=255)]


class ChangeEvidenceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lookback_days: Annotated[int, Field(ge=1, le=730)] = 90
    redeploy_days: Annotated[int, Field(ge=1, le=30)] = 7
    production_environments: Annotated[
        list[EnvironmentName], Field(min_length=1, max_length=20)
    ] = ["production"]

    @field_validator("production_environments")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        names = [name.strip() for name in value]
        if any(not name for name in names):
            raise ValueError("environment names cannot be blank")
        if len(set(names)) != len(names):
            raise ValueError("environment names must be distinct")
        return names


def load(db: Session) -> ChangeEvidenceSettings:
    """The stored settings, or the defaults; a stored value that no longer validates is ignored."""
    stored = read_setting(db, CHANGE_EVIDENCE)
    if stored is None:
        return ChangeEvidenceSettings()
    try:
        return ChangeEvidenceSettings.model_validate(stored)
    except ValidationError:
        return ChangeEvidenceSettings()


def save(db: Session, value: ChangeEvidenceSettings, actor: str) -> None:
    """Store and audit the settings. The caller commits."""
    previous = load(db)
    write_setting(db, CHANGE_EVIDENCE, value.model_dump(mode="json"), actor)
    audit.record(
        db,
        actor=actor,
        action="settings.change_evidence_updated",
        resource_type="setting",
        resource_id=CHANGE_EVIDENCE,
        details={"previous": previous.model_dump(mode="json"), **value.model_dump(mode="json")},
    )
