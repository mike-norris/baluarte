"""How time is collected and kept, stored in the ``time_tracking`` setting (docs/TIME_TRACKING.md).

- ``lookback_days``: how far back a project's first time collection reads (1-730, default 180).
- ``reread_days``: logs spent within this many days are read again each collection, so a log
  deleted in GitLab is marked removed (1-90, default 30).
- ``retention_days``: how long time entries are kept (30-3650), or null to follow the evidence
  retention period. The shorter of the two applies.
- ``missing_time_label``: the label for closed issues with commits but no time spent (default
  ``missing-time``), and ``estimate_round_minutes``: the interval time spent is rounded up to
  when it becomes a closed issue's estimate (5-480, default 30). ``rule_issue_types``: the issue
  types these rules cover (default ``issue`` and ``task``). See ADR-0011.

``BALUARTE_TIME_PER_PERSON`` (``true`` or ``false``, default ``true``) is set only in the
deployment's environment. While it is false no time per person is shown anywhere.

Changing the settings is audited as ``settings.time_tracking_updated``.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.configuration.store import read_setting, write_setting
from baluarte.settings import ConfigurationError, env

TIME_TRACKING = "time_tracking"
PER_PERSON = "BALUARTE_TIME_PER_PERSON"


def per_person_enabled() -> bool:
    """Whether the deployment shows time per person (``BALUARTE_TIME_PER_PERSON``)."""
    raw = env(PER_PERSON).lower()
    if raw in ("", "true"):
        return True
    if raw == "false":
        return False
    raise ConfigurationError(f"{PER_PERSON} must be true or false")


class TimeTrackingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lookback_days: Annotated[int, Field(ge=1, le=730)] = 180
    reread_days: Annotated[int, Field(ge=1, le=90)] = 30
    retention_days: Annotated[int | None, Field(ge=30, le=3650)] = None
    missing_time_label: Annotated[
        str, Field(min_length=1, max_length=255, pattern=r"^[^,\n\r]+$")
    ] = "missing-time"
    estimate_round_minutes: Annotated[int, Field(ge=5, le=480)] = 30
    rule_issue_types: Annotated[
        tuple[Literal["issue", "task", "incident"], ...], Field(min_length=1, max_length=3)
    ] = ("issue", "task")


def load(db: Session) -> TimeTrackingSettings:
    """The stored settings, or the defaults; a stored value that no longer validates is ignored."""
    stored = read_setting(db, TIME_TRACKING)
    if stored is None:
        return TimeTrackingSettings()
    try:
        return TimeTrackingSettings.model_validate(stored)
    except ValidationError:
        return TimeTrackingSettings()


def save(db: Session, value: TimeTrackingSettings, actor: str) -> None:
    """Store and audit the settings. The caller commits."""
    previous = load(db)
    write_setting(db, TIME_TRACKING, value.model_dump(mode="json"), actor)
    audit.record(
        db,
        actor=actor,
        action="settings.time_tracking_updated",
        resource_type="setting",
        resource_id=TIME_TRACKING,
        details={"previous": previous.model_dump(mode="json"), **value.model_dump(mode="json")},
    )
