"""The frameworks, operating settings and controls of the operating profile (docs/COMPLIANCE.md).

Each operating setting has a default, the bounds any value must keep, and what each framework
requires of it. When several frameworks are selected the strictest requirement wins. Each
control maps to the framework references it supports and is either a ``setting`` (met when its
setting meets the requirement), a ``check`` (met when Baluarte observes it, see
``baluarte.compliance.posture``) or an ``attestation`` (met while an administrator's statement is
current, for what Baluarte cannot see).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

FrameworkId = Literal["soc2", "iso27001", "nist_800_53_moderate"]
ControlKind = Literal["setting", "check", "attestation"]
Op = Literal[">=", "<=", "=="]
Value = int | bool

SOC2: FrameworkId = "soc2"
ISO27001: FrameworkId = "iso27001"
NIST: FrameworkId = "nist_800_53_moderate"


@dataclass(frozen=True)
class Framework:
    id: FrameworkId
    name: str


FRAMEWORKS: tuple[Framework, ...] = (
    Framework(SOC2, "SOC 2 Type II (Trust Services Criteria 2017, revised 2022)"),
    Framework(ISO27001, "ISO/IEC 27001:2022 (Annex A)"),
    Framework(NIST, "NIST SP 800-53 Rev. 5, Moderate baseline"),
)
FRAMEWORK_IDS: tuple[FrameworkId, ...] = tuple(framework.id for framework in FRAMEWORKS)


@dataclass(frozen=True)
class Requirement:
    op: Op
    value: Value

    def met_by(self, actual: Value) -> bool:
        if self.op == "==":
            return actual == self.value
        if isinstance(actual, bool) or isinstance(self.value, bool):
            return False
        return actual >= self.value if self.op == ">=" else actual <= self.value


@dataclass(frozen=True)
class SettingSpec:
    name: str
    default: Value
    # Bounds every value must keep, whatever the profile (None for booleans).
    lowest: int | None = None
    highest: int | None = None
    # The environment variable that fixes it, when there is one.
    env: str | None = None


SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("audit_retention_days", 365, 30, 3650),
    SettingSpec("evidence_retention_days", 365, 30, 3650),
    SettingSpec("job_retention_days", 90, 7, 3650),
    SettingSpec("session_idle_minutes", 60, 5, 1440, "BALUARTE_SESSION_IDLE_MINUTES"),
    SettingSpec("session_lifetime_minutes", 480, 15, 10080, "BALUARTE_SESSION_LIFETIME_MINUTES"),
    SettingSpec("require_review", False),
    SettingSpec("legal_hold", False),
    SettingSpec("backup_interval_hours", 24, 1, 168),
)
SETTING_NAMES: tuple[str, ...] = tuple(spec.name for spec in SETTINGS)
SPECS = {spec.name: spec for spec in SETTINGS}

_EVERY = (SOC2, ISO27001, NIST)

# What each framework requires of a setting; a framework that is absent requires nothing.
REQUIREMENTS: dict[str, dict[FrameworkId, Requirement]] = {
    "audit_retention_days": {f: Requirement(">=", 365) for f in _EVERY},
    "evidence_retention_days": {f: Requirement(">=", 365) for f in _EVERY},
    "session_idle_minutes": {
        SOC2: Requirement("<=", 30),
        ISO27001: Requirement("<=", 30),
        NIST: Requirement("<=", 15),
    },
    "session_lifetime_minutes": {f: Requirement("<=", 720) for f in _EVERY},
    "require_review": {f: Requirement("==", True) for f in _EVERY},
    "backup_interval_hours": {f: Requirement("<=", 24) for f in _EVERY},
}


@dataclass(frozen=True)
class Minimum:
    """The strictest requirement of the selected frameworks, and the frameworks that set it."""

    op: Op
    value: Value
    frameworks: tuple[FrameworkId, ...]

    def met_by(self, actual: Value) -> bool:
        return Requirement(self.op, self.value).met_by(actual)


def minimum(setting: str, frameworks: Iterable[FrameworkId]) -> Minimum | None:
    """The requirement in force for ``setting`` under ``frameworks``; None when none applies."""
    selected = set(frameworks)
    applying = [
        (framework, requirement)
        for framework, requirement in REQUIREMENTS.get(setting, {}).items()
        if framework in selected
    ]
    if not applying:
        return None
    op = applying[0][1].op
    values = [requirement.value for _, requirement in applying]
    if op == ">=":
        strictest: Value = max(int(value) for value in values)
    elif op == "<=":
        strictest = min(int(value) for value in values)
    else:
        strictest = values[0]
    binding = tuple(
        framework
        for framework in FRAMEWORK_IDS
        for chosen, requirement in applying
        if chosen == framework and requirement.value == strictest
    )
    return Minimum(op, strictest, binding)


def minimums(frameworks: Iterable[FrameworkId]) -> dict[str, Minimum]:
    selected = tuple(frameworks)
    found = {name: minimum(name, selected) for name in SETTING_NAMES}
    return {name: value for name, value in found.items() if value is not None}


@dataclass(frozen=True)
class Control:
    id: str
    title: str
    area: str
    kind: ControlKind
    description: str
    how_to: str
    mappings: dict[FrameworkId, tuple[str, ...]]
    # ``setting`` controls: the operating setting they govern.
    setting: str | None = None
    # How long an attestation stays current: required for ``attestation`` controls, and on a
    # ``check`` it allows an attestation as the alternative way to meet it.
    attestation_days: int | None = None

    def requirements(self) -> dict[FrameworkId, Requirement] | None:
        return REQUIREMENTS.get(self.setting) if self.setting is not None else None


def _maps(
    soc2: Sequence[str], iso: Sequence[str], nist: Sequence[str]
) -> dict[FrameworkId, tuple[str, ...]]:
    return {SOC2: tuple(soc2), ISO27001: tuple(iso), NIST: tuple(nist)}


CONTROLS: tuple[Control, ...] = (
    Control(
        "audit-retention",
        "Audit trail retention",
        "Logging and audit",
        "setting",
        "Audit events are kept for at least the period an auditor examines, so every "
        "administrative action in it can be shown.",
        "Set **Audit events** in Settings > Compliance to the required number of days or more. "
        "Events older than this are deleted by the daily retention job unless legal hold is on.",
        _maps(["CC7.2"], ["A.8.15"], ["AU-11"]),
        setting="audit_retention_days",
    ),
    Control(
        "evidence-retention",
        "Evidence retention",
        "Data retention",
        "setting",
        "Observations, evaluations and collection runs are kept long enough to support every "
        "finding and export in the audit period.",
        "Set **Evidence** in Settings > Compliance to the required number of days or more. The "
        "newest observation of each target and anything a finding refers to are always kept.",
        _maps(["CC4.1"], ["A.5.33"], ["AU-11", "SI-12"]),
        setting="evidence_retention_days",
    ),
    Control(
        "session-idle",
        "Idle session timeout",
        "Access",
        "setting",
        "A browser session that is not used ends after a short time.",
        "Set **Idle timeout** in Settings > Compliance. When the deployment sets "
        "`BALUARTE_SESSION_IDLE_MINUTES`, change it there instead.",
        _maps(["CC6.1"], ["A.8.5"], ["AC-11", "AC-12"]),
        setting="session_idle_minutes",
    ),
    Control(
        "session-lifetime",
        "Session lifetime",
        "Access",
        "setting",
        "Every session ends after a fixed time, however it is used, and the user signs in again.",
        "Set **Session lifetime** in Settings > Compliance. When the deployment sets "
        "`BALUARTE_SESSION_LIFETIME_MINUTES`, change it there instead.",
        _maps(["CC6.1"], ["A.8.5"], ["AC-12"]),
        setting="session_lifetime_minutes",
    ),
    Control(
        "change-review",
        "Two-person review of baseline changes",
        "Change management",
        "setting",
        "A new baseline version, assignment or unassignment takes effect only after someone "
        "other than the person who proposed it approves it.",
        "Turn on **Require review** in Settings > Compliance. At least two people need the "
        "admin or analyst role to make baseline changes once it is on.",
        _maps(["CC8.1"], ["A.8.32"], ["CM-3"]),
        setting="require_review",
    ),
    Control(
        "audit-integrity",
        "Audit trail integrity",
        "Logging and audit",
        "check",
        "Every audit event still matches the SHA-256 digest recorded when it was written.",
        "A mismatch means an audit event was changed after it was written. Treat it as a "
        "security incident: preserve the database, compare it with backups and with the "
        "shipped `baluarte.audit` log, and find who had database access.",
        _maps(["CC7.2"], ["A.8.15"], ["AU-9"]),
    ),
    Control(
        "audit-protection",
        "Audit trail protected from the application",
        "Logging and audit",
        "check",
        "The database role the application uses cannot change or delete audit events: it is "
        "not a superuser, does not own the audit table and holds no UPDATE, DELETE or TRUNCATE "
        "privilege on it. Expiry goes only through the retention function.",
        "Run the API and worker as the data-only role that `baluarte-migrate` creates "
        "(`BALUARTE_APP_DATABASE_USER`), and run migrations as the schema owner. See "
        "docs/DEPLOYMENT.md#database-roles.",
        _maps(["CC7.2"], ["A.8.15"], ["AU-9"]),
    ),
    Control(
        "secrets-sealed",
        "Stored secrets are encrypted",
        "Cryptography",
        "check",
        "A secret key is configured, so the GitLab token and identity provider secret entered "
        "in the web UI are sealed with AES-256-GCM before they are stored.",
        "Create a key and set `BALUARTE_SECRET_KEY_FILE` (Helm: `secretKey.existingSecret`). "
        "See docs/SECURITY.md.",
        _maps(["CC6.1"], ["A.8.24"], ["SC-12", "SC-28"]),
    ),
    Control(
        "tls-web",
        "Web interface uses TLS",
        "Cryptography",
        "check",
        "Baluarte's public URL is https, so sessions and data travel encrypted and session "
        "cookies are Secure.",
        "Put Baluarte behind a TLS-terminating reverse proxy or ingress and set the public URL "
        "to its https address in Settings > Identity provider (or `BALUARTE_PUBLIC_URL`).",
        _maps(["CC6.7"], ["A.5.14", "A.8.24"], ["SC-8"]),
    ),
    Control(
        "tls-database",
        "Database connection uses TLS",
        "Cryptography",
        "check",
        "The connection to PostgreSQL is encrypted, and the URL requires it (sslmode require, "
        "verify-ca or verify-full), so it cannot silently fall back to plain text.",
        "Enable TLS on PostgreSQL and add `?sslmode=verify-full` to `BALUARTE_DATABASE_URL` "
        "(Helm: `database.sslMode`).",
        _maps(["CC6.7"], ["A.5.14", "A.8.24"], ["SC-8"]),
    ),
    Control(
        "tls-gitlab",
        "GitLab connection uses TLS",
        "Cryptography",
        "check",
        "Baluarte reaches GitLab over https, so the token and collected evidence travel encrypted.",
        "Use the https URL of the GitLab instance in Settings > GitLab.",
        _maps(["CC6.7"], ["A.5.14", "A.8.24"], ["SC-8"]),
    ),
    Control(
        "retention-running",
        "Retention is enforced",
        "Data retention",
        "check",
        "The daily retention job has succeeded in the last 48 hours, so data is not kept longer "
        "than the profile says (unless legal hold is on).",
        "Check that the worker is running and look for failed `retention.expire` jobs on the "
        "Jobs page.",
        _maps(["CC7.2"], ["A.5.33"], ["SI-12"]),
    ),
    Control(
        "collection-health",
        "GitLab collection is healthy",
        "Monitoring",
        "check",
        "Scheduled collection from GitLab is succeeding, so findings reflect GitLab as it is.",
        "Follow the collection alert on the dashboard: check that Baluarte can reach GitLab, "
        "that the token is valid and that the worker is running.",
        _maps(["CC7.1"], ["A.8.16"], ["CA-7"]),
    ),
    Control(
        "inventory-population",
        "Inventory covers the whole instance",
        "Monitoring",
        "check",
        "The GitLab token can read every group and project, so no project escapes evaluation.",
        "Use a token of a GitLab administrator with the `read_api` and `admin_mode` scopes. "
        "See docs/GITLAB_INTEGRATION.md.",
        _maps(["CC4.1"], ["A.8.16"], ["CA-7", "CM-8"]),
    ),
    Control(
        "log-shipping",
        "Audit events are shipped off the host",
        "Logging and audit",
        "check",
        "Every audit event is copied, in order and without gaps, to a log store outside "
        "Baluarte and its database (a SIEM over syslog with TLS, or Elasticsearch/OpenSearch), "
        "where Baluarte's own database role cannot change it.",
        "Turn on **Audit log shipping** in Settings and send it to your SIEM. Baluarte ships "
        "from the database with a cursor, so nothing is lost while the destination is down. If "
        "your platform's log collector already ships the `baluarte.audit` log stream, attest to "
        "it here instead, naming the destination.",
        _maps(["CC7.2"], ["A.8.15"], ["AU-4", "AU-9"]),
        attestation_days=90,
    ),
    Control(
        "backup-frequency",
        "Backups run often enough",
        "Resilience",
        "setting",
        "The database is backed up at least as often as the profile requires, so no more than "
        "that much evidence and audit history can be lost.",
        "Set **Backup interval** in Settings > Compliance, and run the backup service with the "
        "same interval (Compliance > Services gives the configuration).",
        _maps(["A1.2"], ["A.8.13"], ["CP-9"]),
        setting="backup_interval_hours",
    ),
    Control(
        "backups",
        "Backups are taken and encrypted",
        "Resilience",
        "check",
        "The backup service has reported an encrypted backup within the backup interval, plus "
        "two hours' grace.",
        "Run the `baluarte-backup` service (Compose: the `backup` profile; Helm: "
        "`backup.enabled`) with an age public key, and keep the private key offline. "
        "Compliance > Services has the exact commands.",
        _maps(["A1.2"], ["A.8.13"], ["CP-9"]),
    ),
    Control(
        "backup-offsite",
        "Backups are copied off the database host",
        "Resilience",
        "attestation",
        "The encrypted backup files are copied to storage outside the database host and outside "
        "the reach of anyone who can delete the database.",
        "Mount the backup directory on network storage, or copy it off the host on a schedule "
        "(object storage with versioning or retention lock is best). Then attest to it here, "
        "naming the location.",
        _maps(["A1.2"], ["A.8.13"], ["CP-9"]),
        attestation_days=90,
    ),
    Control(
        "restore-test",
        "A restore was tested in the last 12 months",
        "Resilience",
        "attestation",
        "A backup was restored into a scratch database in the last 12 months and the result was "
        "checked, including the audit trail's digests.",
        "Decrypt a backup with the age private key, restore it into a scratch database and run "
        "the checks in docs/OPERATIONS.md (Verifying a restore). Then attest to it here, naming "
        "the backup and the date.",
        _maps(["A1.3"], ["A.8.13"], ["CP-4"]),
        attestation_days=365,
    ),
    Control(
        "encryption-at-rest",
        "Database storage is encrypted",
        "Cryptography",
        "attestation",
        "The disks or volumes holding the database and its backups are encrypted.",
        "Use an encrypted volume or a managed database with storage encryption. Then attest to "
        "it here, naming the mechanism.",
        _maps(["CC6.1"], ["A.8.24"], ["SC-28"]),
        attestation_days=365,
    ),
    Control(
        "identity-mfa",
        "Sign-in requires multi-factor authentication",
        "Access",
        "attestation",
        "The identity provider Baluarte uses requires multi-factor authentication for everyone "
        "who can sign in to Baluarte.",
        "Require MFA in the identity provider for the groups mapped to Baluarte roles. Then "
        "attest to it here.",
        _maps(["CC6.1"], ["A.8.5"], ["IA-2(1)", "IA-2(2)"]),
        attestation_days=365,
    ),
    Control(
        "access-review",
        "Access is reviewed",
        "Access",
        "attestation",
        "Membership of the identity provider groups mapped to Baluarte's admin, analyst and "
        "auditor roles was reviewed and anyone who no longer needs access was removed.",
        "Review the members of each mapped group (Settings > Identity provider lists them), "
        "remove anyone who no longer needs access, then attest to it here.",
        _maps(["CC6.2", "CC6.3"], ["A.5.18"], ["AC-2"]),
        attestation_days=90,
    ),
)
CONTROLS_BY_ID = {control.id: control for control in CONTROLS}
CONTROL_FOR_SETTING = {
    control.setting: control for control in CONTROLS if control.setting is not None
}
