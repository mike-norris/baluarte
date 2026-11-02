"""The operating profile in force: the frameworks chosen and the operating settings.

Stored in the ``operations`` setting, except ``require_review``, which stays in the
``governance`` setting that baseline changes read (``baluarte.policy.changes``). A session limit
the environment sets wins and cannot be changed here; its source is ``deployment``.

Changing the profile is refused when a value misses the minimum of the selected frameworks and
no exception is in force for its control (``baluarte.compliance.exceptions``).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.auth.settings import deployment_session_minutes
from baluarte.compliance.catalogue import (
    CONTROL_FOR_SETTING,
    FRAMEWORK_IDS,
    SETTING_NAMES,
    SPECS,
    FrameworkId,
    Minimum,
    Value,
    minimums,
)
from baluarte.configuration.store import OPERATIONS, read_setting, write_setting
from baluarte.db.models import StoredSetting

GOVERNANCE = "governance"
REVIEW = "require_review"

SettingSource = Literal["deployment", "database", "default"]


@dataclass(frozen=True)
class Violation:
    control_id: str
    setting: str
    value: Value
    required: Minimum

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "setting": self.setting,
            "value": self.value,
            "required": {"op": self.required.op, "value": self.required.value},
            "frameworks": list(self.required.frameworks),
        }


class ProfileInvalid(Exception):
    """The values are invalid (HTTP 422)."""


class BelowMinimum(Exception):
    """A value misses the profile's minimum and no exception is in force (HTTP 409)."""

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__("A setting is below the minimum of the selected frameworks")
        self.violations = violations


class DeploymentSetting(Exception):
    """The request changes a setting the environment fixes (HTTP 409)."""

    def __init__(self, settings: list[str]) -> None:
        super().__init__("The deployment's environment sets " + ", ".join(settings))
        self.settings = settings


@dataclass(frozen=True)
class Profile:
    frameworks: tuple[FrameworkId, ...]
    configured: bool
    settings: dict[str, Value]
    sources: dict[str, SettingSource]
    updated_by: str | None
    updated_at: datetime | None

    def minimums(self) -> dict[str, Minimum]:
        return minimums(self.frameworks)


def today() -> date:
    return datetime.now(UTC).date()


def ordered(frameworks: list[str] | tuple[str, ...]) -> tuple[FrameworkId, ...]:
    """Frameworks deduplicated in catalogue order; ProfileInvalid for an unknown one."""
    unknown = sorted(set(frameworks) - set(FRAMEWORK_IDS))
    if unknown:
        raise ProfileInvalid("Unknown framework: " + ", ".join(unknown))
    return tuple(framework for framework in FRAMEWORK_IDS if framework in frameworks)


def deployment_values() -> dict[str, int]:
    """The settings the environment fixes."""
    idle, lifetime = deployment_session_minutes()
    fixed = {"session_idle_minutes": idle, "session_lifetime_minutes": lifetime}
    return {name: value for name, value in fixed.items() if value is not None}


def stored_review(db: Session) -> bool | None:
    value = read_setting(db, GOVERNANCE)
    return None if value is None else bool(value.get(REVIEW, False))


def load(db: Session) -> Profile:
    row = db.get(StoredSetting, OPERATIONS)
    stored: dict[str, Any] = dict(row.value) if row is not None else {}
    stored_settings: dict[str, Any] = stored.get("settings") or {}
    fixed = deployment_values()
    review = stored_review(db)
    settings: dict[str, Value] = {}
    sources: dict[str, SettingSource] = {}
    for name in SETTING_NAMES:
        if name in fixed:
            settings[name], sources[name] = fixed[name], "deployment"
        elif name == REVIEW:
            settings[name] = review if review is not None else bool(SPECS[name].default)
            sources[name] = "database" if review is not None else "default"
        elif name in stored_settings:
            settings[name], sources[name] = stored_settings[name], "database"
        else:
            settings[name], sources[name] = SPECS[name].default, "default"
    frameworks = stored.get("frameworks") or []
    return Profile(
        frameworks=tuple(f for f in FRAMEWORK_IDS if f in frameworks),
        configured=row is not None,
        settings=settings,
        sources=sources,
        updated_by=row.updated_by if row is not None else None,
        updated_at=row.updated_at if row is not None else None,
    )


def _checked(settings: dict[str, Any]) -> dict[str, Value]:
    missing = [name for name in SETTING_NAMES if name not in settings]
    if missing:
        raise ProfileInvalid("Missing settings: " + ", ".join(missing))
    checked: dict[str, Value] = {}
    for name in SETTING_NAMES:
        spec, value = SPECS[name], settings[name]
        if isinstance(spec.default, bool):
            if not isinstance(value, bool):
                raise ProfileInvalid(f"{name} must be true or false")
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ProfileInvalid(f"{name} must be a whole number")
        elif (
            spec.lowest is not None
            and spec.highest is not None
            and not (spec.lowest <= value <= spec.highest)
        ):
            raise ProfileInvalid(f"{name} must be between {spec.lowest} and {spec.highest}")
        checked[name] = value
    if checked["session_idle_minutes"] > checked["session_lifetime_minutes"]:
        raise ProfileInvalid("session_idle_minutes cannot exceed session_lifetime_minutes")
    return checked


def violations(
    db: Session,
    frameworks: tuple[FrameworkId, ...],
    settings: dict[str, Value],
    *,
    ignore: frozenset[str] = frozenset(),
) -> list[Violation]:
    """Settings below the frameworks' minimum whose control has no exception in force."""
    # Imported here: exceptions reads the profile to decide whether review is required.
    from baluarte.compliance.exceptions import controls_in_force

    excepted = controls_in_force(db)
    found = []
    for name, required in minimums(frameworks).items():
        control = CONTROL_FOR_SETTING[name]
        if name in ignore or control.id in excepted or required.met_by(settings[name]):
            continue
        found.append(Violation(control.id, name, settings[name], required))
    return found


def update(db: Session, frameworks: list[str], settings: dict[str, Any], actor: str) -> Profile:
    """Validate, store and audit a new profile. The caller commits and invalidates the cache.

    A setting left out keeps its current value, so a client that predates a setting can still
    save the profile.
    """
    chosen = ordered(frameworks)
    current = load(db).settings
    checked = _checked({**current, **settings})
    fixed = deployment_values()
    changed_fixed = sorted(name for name, value in fixed.items() if checked[name] != value)
    if changed_fixed:
        raise DeploymentSetting(changed_fixed)
    found = violations(db, chosen, checked, ignore=frozenset(fixed))
    if found:
        raise BelowMinimum(found)

    previous = load(db)
    stored = {name: value for name, value in checked.items() if name != REVIEW}
    write_setting(db, OPERATIONS, {"frameworks": list(chosen), "settings": stored}, actor)
    if previous.settings[REVIEW] != checked[REVIEW]:
        write_review(db, bool(checked[REVIEW]), actor)
    audit.record(
        db,
        actor=actor,
        action="settings.compliance_updated",
        resource_type="settings",
        resource_id=OPERATIONS,
        details={
            "frameworks": list(chosen),
            "settings": checked,
            "previous": {
                "configured": previous.configured,
                "frameworks": list(previous.frameworks),
                "settings": previous.settings,
            },
        },
    )
    db.flush()
    return load(db)


def write_review(db: Session, required: bool, actor: str) -> None:
    """Store the ``governance`` setting and audit it. The caller checks the profile allows it."""
    previous = stored_review(db)
    write_setting(db, GOVERNANCE, {REVIEW: required}, actor)
    audit.record(
        db,
        actor=actor,
        action="settings.governance_updated",
        resource_type="settings",
        resource_id=GOVERNANCE,
        details={REVIEW: required, "previous": bool(previous)},
    )


def review_violation(db: Session, required: bool) -> Violation | None:
    """Why the profile refuses turning review off, or None when it allows ``required``."""
    if required:
        return None
    profile = load(db)
    found = violations(db, profile.frameworks, {**profile.settings, REVIEW: False})
    return next((v for v in found if v.setting == REVIEW), None)


def session_minutes(db: Session) -> tuple[int, int]:
    """The idle and absolute session limits in force, in minutes."""
    profile = load(db)
    return (
        int(profile.settings["session_idle_minutes"]),
        int(profile.settings["session_lifetime_minutes"]),
    )
