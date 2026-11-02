"""The services the operating profile needs beside the API and worker, whether they are seen
working, and how to run them where Baluarte cannot start them itself.

Baluarte never starts a container: it reports what it can observe (backup runs, shipping
progress) and gives the exact Compose and Helm steps and configuration.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from baluarte.compliance import backups, operations, shipping
from baluarte.compliance.posture import Posture

SHIPPING_COMPOSE = """Audit log shipping runs inside the Baluarte worker; there is no extra \
container to start.

1. Open **Settings > Audit log shipping**.
2. Choose **Syslog over TLS** (port 6514, RFC 5425) or **Elasticsearch / OpenSearch** (https).
3. Paste the CA certificate if the destination's certificate is not signed by a public CA.
4. Select **Test**, then turn shipping on and save.

The worker must reach the destination: allow outbound TCP from the `worker` service to it.

If your platform's log collector already ships the worker's `baluarte.audit` log stream to your \
SIEM, leave shipping off and attest to it under **Audit events are shipped off the host**.
"""

SHIPPING_HELM = """Audit log shipping runs inside the Baluarte worker; there is no extra \
workload to deploy.

1. Open **Settings > Audit log shipping**.
2. Choose **Syslog over TLS** (port 6514, RFC 5425) or **Elasticsearch / OpenSearch** (https).
3. Paste the CA certificate if the destination's certificate is not signed by a public CA.
4. Select **Test**, then turn shipping on and save.

If a NetworkPolicy restricts egress, allow the worker pods to reach the destination.

If a cluster log collector already ships the worker's `baluarte.audit` log stream to your SIEM, \
leave shipping off and attest to it under **Audit events are shipped off the host**.
"""


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    purpose: str
    required: bool
    detected: bool
    healthy: bool
    detail: str
    how_to: dict[str, str]
    config: dict[str, dict[str, str]] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "purpose": self.purpose,
            "required": self.required,
            "detected": self.detected,
            "healthy": self.healthy,
            "detail": self.detail,
            "how_to": self.how_to,
            "config": self.config,
        }


def _control(result: Posture, control_id: str) -> tuple[str, str]:
    """A control's own result and detail, whatever the profile makes of it."""
    for control in result.controls:
        if control.control.id == control_id:
            own = control.evidence.get("result", control.status)
            return str(own), control.detail
    return "not_met", ""


def _backup(db: Session, profile: operations.Profile, result: Posture) -> Service:
    hours = int(profile.settings["backup_interval_hours"])
    detected = bool(backups.recent(db, 1))
    own, detail = _control(result, "backups")
    return Service(
        id="backup",
        name="Database backups (baluarte-backup)",
        purpose="Takes an encrypted pg_dump of the database every "
        f"{hours} hours and reports each run, so evidence and the audit trail survive losing "
        "the database.",
        required=bool(profile.frameworks),
        detected=detected,
        healthy=own == "met",
        detail=detail if detected else "No backup run has been reported yet.",
        how_to={"compose": backups.compose_how_to(hours), "helm": backups.helm_how_to(hours)},
        config={
            "compose": {"file_name": "baluarte-backup.env", "content": backups.compose_env(hours)},
            "helm": {"file_name": "backup-values.yaml", "content": backups.helm_values(hours)},
        },
    )


def _shipping(db: Session, profile: operations.Profile, result: Posture) -> Service:
    state = shipping.status(db)
    own, detail = _control(result, "log-shipping")
    return Service(
        id="audit-shipping",
        name="Audit log shipping",
        purpose="Copies every audit event to your SIEM or log store, so the audit trail is kept "
        "outside Baluarte and its database.",
        # Not needed when an attestation says the platform's collector ships them.
        required=bool(profile.frameworks) and not (own == "met" and not state.enabled),
        detected=state.enabled,
        healthy=state.enabled and own == "met",
        detail=detail,
        how_to={"compose": SHIPPING_COMPOSE, "helm": SHIPPING_HELM},
        config=None,
    )


def services(db: Session, result: Posture) -> list[Service]:
    profile = operations.load(db)
    return [_backup(db, profile, result), _shipping(db, profile, result)]
