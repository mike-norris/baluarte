"""Provider connections in a workspace (ADR-0013, roadmap M11.3).

A connection is one GitLab deployment Baluarte collects from. There are two kinds:

- **The primary connection:** the GitLab set in the deployment's environment or on the Settings
  page, exactly as before. Its settings and token stay where they were.
- **Added connections:** created on the Connections page (``/api/v1/connections``). Their settings
  live on the ``gitlab_instance`` row and their token is sealed as ``connection_token:<id>``.

Every collection job names the connection it collects (``CONNECTION`` in its payload); a job
without one collects the primary connection, as jobs queued before this change do.

Connection URLs are untrusted input. An added connection's host must resolve only to public
addresses, unless an administrator allows a private network for it; loopback, link-local
(including cloud metadata addresses), multicast and unspecified addresses are always refused.
The check runs when a connection is added or changed and before each collection, so a name that
later resolves elsewhere is refused too. It does not apply to the primary connection, whose URL is
set by the deployment or an administrator as before.
"""

import ipaddress
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.configuration.crypto import SecretBox, SecretUnavailable, load_secret_box
from baluarte.configuration.store import delete_secret, read_secret_value, write_secret
from baluarte.db.models import GitLabInstance
from baluarte.integrations.gitlab.settings import (
    GitLabSettings,
    checked_interval,
    checked_timeout,
    gitlab_url,
)
from baluarte.jobs.queue import PermanentJobError
from baluarte.settings import ConfigurationError
from baluarte.workspaces import ALL_WORKSPACES, configured_settings, current_workspace_id

# The job payload key that names the connection a job collects.
CONNECTION = "connection_id"

Resolver = Callable[[str], Sequence[str]]

if TYPE_CHECKING:
    from baluarte.integrations.github.client import Auth


class ConnectionRefused(ValueError):
    """The connection cannot be used, with a reason safe to show to an administrator."""


def token_name(connection_id: int) -> str:
    return f"connection_token:{connection_id}"


def dns_lookup(host: str) -> list[str]:
    """The addresses ``host`` resolves to; tests replace this function."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ConnectionRefused(f"{host} could not be resolved") from exc
    return sorted({str(info[4][0]) for info in infos})


def check_url(url: str, allow_private_network: bool, resolve: Resolver | None = None) -> None:
    """Refuse a URL whose host resolves to an address Baluarte must not call.

    Raises ConnectionRefused naming the host and why.
    """
    host = urlsplit(url).hostname
    if not host:
        raise ConnectionRefused("The URL has no host")
    addresses = (resolve or dns_lookup)(host)
    if not addresses:
        raise ConnectionRefused(f"{host} could not be resolved")
    for raw in addresses:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise ConnectionRefused(f"{host} resolves to {address}, which is never allowed")
        if not address.is_global and not allow_private_network:
            raise ConnectionRefused(
                f"{host} resolves to the private address {address}; allow a private network "
                "for this connection if it is a self-managed host"
            )


def is_primary(connection: GitLabInstance, primary: GitLabSettings | None) -> bool:
    return primary is not None and connection.base_url == primary.url


@dataclass(frozen=True)
class Resolved:
    connection: GitLabInstance
    settings: GitLabSettings
    primary: bool


def settings_for(
    db: Session,
    connection_id: int | None,
    box: SecretBox | None,
    resolve: Resolver | None = None,
) -> Resolved:
    """The settings a job collects one connection with.

    ``connection_id`` None means the primary connection. Raises ConnectionRefused when the
    connection is unknown, removed, has no token, or (for an added connection) its URL fails the
    address check.
    """
    primary = configured_settings()
    if connection_id is None:
        if primary is None:
            raise ConnectionRefused("GitLab is not configured")
        connection = db.scalars(
            select(GitLabInstance).where(GitLabInstance.base_url == primary.url)
        ).first()
        if connection is None:
            # Not collected yet: the first job creates the row (``instance_for``).
            raise _NotYetRecorded(primary)
        return Resolved(connection, primary, True)
    connection = db.get(GitLabInstance, connection_id)
    if connection is None or connection.removed_at is not None:
        raise ConnectionRefused(f"Connection {connection_id} does not exist or was removed")
    if connection.provider != "gitlab":
        raise ConnectionRefused(f"Connection {connection_id} is not a GitLab connection")
    if primary is not None and is_primary(connection, primary):
        return Resolved(connection, primary, True)
    token = read_secret_value(db, token_name(connection.id), box)
    if token is None:
        raise ConnectionRefused(f"Connection {connection_id} has no token")
    check_url(connection.base_url, connection.allow_private_network, resolve)
    settings = GitLabSettings(
        url=connection.base_url,
        token=token,
        timeout_seconds=connection.timeout_seconds,
        sync_interval_minutes=connection.sync_interval_minutes,
        source="database",
    )
    return Resolved(connection, settings, False)


class _NotYetRecorded(ConnectionRefused):
    """The primary connection is configured but has no row yet."""

    def __init__(self, settings: GitLabSettings) -> None:
        super().__init__("The primary connection has not been collected yet")
        self.settings = settings


def job_settings(
    db: Session, connection_id: int | None, box: SecretBox | None, resolve: Resolver | None = None
) -> GitLabSettings:
    """The settings for a job's connection; the primary's even before its first collection."""
    try:
        return settings_for(db, connection_id, box, resolve).settings
    except _NotYetRecorded as pending:
        return pending.settings


def github_connection(
    db: Session, connection_id: int, box: SecretBox | None
) -> tuple[GitLabInstance, "Auth", float]:
    """A GitHub connection with the credential to collect it and its timeout.

    Raises ConnectionRefused when the connection is unknown, removed, not GitHub's, or has no
    credential stored. The token or the App's private key is sealed as ``connection_token:<id>``.
    """
    from baluarte.integrations.github.client import AppAuth, TokenAuth

    connection = db.get(GitLabInstance, connection_id)
    if connection is None or connection.removed_at is not None:
        raise ConnectionRefused(f"Connection {connection_id} does not exist or was removed")
    if connection.provider != "github":
        raise ConnectionRefused(f"Connection {connection_id} is not a GitHub connection")
    secret = read_secret_value(db, token_name(connection.id), box)
    if secret is None:
        raise ConnectionRefused(f"Connection {connection_id} has no credential")
    auth: Auth
    if connection.auth_kind == "app":
        if connection.app_id is None or connection.installation_id is None:
            raise ConnectionRefused(f"Connection {connection_id} has no App installation")
        auth = AppAuth(connection.app_id, connection.installation_id, secret)
    else:
        auth = TokenAuth(secret)
    return connection, auth, connection.timeout_seconds


def active(db: Session) -> list[GitLabInstance]:
    """Connections that are not removed, oldest first."""
    return list(
        db.scalars(
            select(GitLabInstance)
            .where(GitLabInstance.removed_at.is_(None))
            .order_by(GitLabInstance.id)
        )
    )


def store_token(db: Session, connection_id: int, token: str, box: SecretBox, actor: str) -> None:
    write_secret(db, token_name(connection_id), token, box, actor)


def forget_token(db: Session, connection_id: int) -> None:
    delete_secret(db, token_name(connection_id))


def connection_of(payload: Mapping[str, Any] | None) -> int | None:
    """The connection a job's payload names; None means the primary connection."""
    value = (payload or {}).get(CONNECTION)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def payload_for(connection_id: int | None) -> dict[str, Any] | None:
    """The payload that makes a follow-up job collect the same connection."""
    return {CONNECTION: connection_id} if connection_id is not None else None


def settings_for_job(engine: Engine, payload: Mapping[str, Any] | None) -> GitLabSettings:
    """The settings a job collects with; PermanentJobError when its connection cannot be used.

    The primary connection's settings come from the deployment or the Settings page as before,
    so a configuration error there is reported the same way.
    """
    connection_id = connection_of(payload)
    try:
        with Session(engine) as db:
            return job_settings(db, connection_id, load_secret_box() if connection_id else None)
    except (ConnectionRefused, ConfigurationError, SecretUnavailable) as exc:
        raise PermanentJobError(str(exc)) from exc


# Adding, changing and removing connections (the Connections page). Every change is audited.


class ConnectionConflict(Exception):
    """The change is not allowed in the current state (HTTP 409)."""


def _checked(url: str, timeout: float, interval: int) -> tuple[str, float, int]:
    try:
        return (
            gitlab_url(url.strip(), "url"),
            checked_timeout(timeout, "timeout_seconds"),
            checked_interval(interval, "sync_interval_minutes"),
        )
    except ConfigurationError as exc:
        raise ConnectionRefused(str(exc)) from exc


def _box() -> SecretBox:
    try:
        box = load_secret_box()
    except ConfigurationError as exc:
        raise ConnectionConflict(str(exc)) from exc
    if box is None:
        raise ConnectionConflict(
            "BALUARTE_SECRET_KEY_FILE is not configured, so a connection's token cannot be stored"
        )
    return box


def add(
    db: Session,
    *,
    name: str,
    url: str,
    token: str,
    timeout_seconds: float,
    sync_interval_minutes: int,
    allow_private_network: bool,
    actor: str,
    resolve: Resolver | None = None,
) -> GitLabInstance:
    """Add a GitLab connection to the current workspace; the caller commits.

    Adding the URL of a removed connection restores it, with its data, and the new settings.
    """
    url, timeout, interval = _checked(url, timeout_seconds, sync_interval_minutes)
    if not token.strip():
        raise ConnectionRefused("A read_api token is required")
    primary = configured_settings()
    if primary is not None and primary.url == url:
        raise ConnectionConflict("This is the primary connection, set on the Settings page")
    existing = db.scalars(
        select(GitLabInstance)
        .where(GitLabInstance.base_url == url)
        .execution_options(**{ALL_WORKSPACES: True})
    ).first()
    if existing is not None and existing.workspace_id != current_workspace_id(db):
        raise ConnectionConflict("This URL is connected in another workspace")
    if existing is not None and existing.removed_at is None:
        raise ConnectionConflict("A connection to this URL already exists")
    box = _box()
    check_url(url, allow_private_network, resolve)
    connection = existing or GitLabInstance(
        base_url=url, provider="gitlab", workspace_id=current_workspace_id(db)
    )
    connection.name = name.strip() or None
    connection.timeout_seconds = timeout
    connection.sync_interval_minutes = interval
    connection.allow_private_network = allow_private_network
    connection.paused = False
    connection.removed_at = None
    db.add(connection)
    db.flush()
    store_token(db, connection.id, token, box, actor)
    audit.record(
        db,
        actor=actor,
        action="connection.added" if existing is None else "connection.restored",
        resource_type="gitlab_instance",
        resource_id=str(connection.id),
        details={
            "url": url,
            "name": connection.name,
            "timeout_seconds": timeout,
            "sync_interval_minutes": interval,
            "allow_private_network": allow_private_network,
        },
    )
    return connection


def add_github(
    db: Session,
    *,
    name: str,
    organization: str,
    auth_kind: str,
    secret: str,
    app_id: int | None,
    installation_id: int | None,
    timeout_seconds: float,
    sync_interval_minutes: int,
    actor: str,
) -> GitLabInstance:
    """Add a GitHub.com organization to the current workspace; the caller commits.

    ``secret`` is a fine-grained token (``auth_kind`` ``token``) or the GitHub App's private key
    (``app``, with ``app_id`` and ``installation_id``). Adding a removed organization again
    restores it, with its data.
    """
    from baluarte.integrations.github.client import connection_url

    try:
        url = connection_url(organization.strip())
        timeout = checked_timeout(timeout_seconds, "timeout_seconds")
        interval = checked_interval(sync_interval_minutes, "sync_interval_minutes")
    except (ValueError, ConfigurationError) as exc:
        raise ConnectionRefused(str(exc)) from exc
    if auth_kind not in ("token", "app"):
        raise ConnectionRefused("auth_kind is token or app")
    if not secret.strip():
        raise ConnectionRefused(
            "A private key is required" if auth_kind == "app" else "A token is required"
        )
    if auth_kind == "app" and (app_id is None or installation_id is None):
        raise ConnectionRefused("A GitHub App connection needs app_id and installation_id")
    existing = db.scalars(
        select(GitLabInstance)
        .where(GitLabInstance.base_url == url)
        .execution_options(**{ALL_WORKSPACES: True})
    ).first()
    if existing is not None and existing.workspace_id != current_workspace_id(db):
        raise ConnectionConflict("This organization is connected in another workspace")
    if existing is not None and existing.removed_at is None:
        raise ConnectionConflict("A connection to this organization already exists")
    box = _box()
    connection = existing or GitLabInstance(
        base_url=url, provider="github", workspace_id=current_workspace_id(db)
    )
    connection.name = name.strip() or None
    connection.auth_kind = auth_kind
    connection.app_id = app_id if auth_kind == "app" else None
    connection.installation_id = installation_id if auth_kind == "app" else None
    connection.timeout_seconds = timeout
    connection.sync_interval_minutes = interval
    connection.allow_private_network = False
    connection.paused = False
    connection.removed_at = None
    db.add(connection)
    db.flush()
    store_token(db, connection.id, secret, box, actor)
    audit.record(
        db,
        actor=actor,
        action="connection.added" if existing is None else "connection.restored",
        resource_type="gitlab_instance",
        resource_id=str(connection.id),
        details={
            "provider": "github",
            "url": url,
            "name": connection.name,
            "auth_kind": auth_kind,
            "app_id": connection.app_id,
            "installation_id": connection.installation_id,
            "timeout_seconds": timeout,
            "sync_interval_minutes": interval,
        },
    )
    return connection


def change(
    db: Session,
    connection_id: int,
    *,
    actor: str,
    name: str | None = None,
    token: str | None = None,
    timeout_seconds: float | None = None,
    sync_interval_minutes: int | None = None,
    allow_private_network: bool | None = None,
    paused: bool | None = None,
    resolve: Resolver | None = None,
) -> GitLabInstance:
    """Change a connection; the caller commits.

    The primary connection's URL, token and timing stay on the Settings page; only its name and
    whether it is paused change here. A URL never changes: add a new connection instead.
    """
    connection = db.get(GitLabInstance, connection_id)
    if connection is None or connection.removed_at is not None:
        raise LookupError(connection_id)
    primary = is_primary(connection, configured_settings())
    if primary and (
        token is not None
        or timeout_seconds is not None
        or sync_interval_minutes is not None
        or allow_private_network is not None
    ):
        raise ConnectionConflict(
            "The primary connection's token and timing are set on the Settings page"
        )
    changed: dict[str, Any] = {}
    if name is not None:
        connection.name = changed["name"] = name.strip() or None
    if timeout_seconds is not None or sync_interval_minutes is not None:
        _, timeout, interval = _checked(
            connection.base_url,
            connection.timeout_seconds if timeout_seconds is None else timeout_seconds,
            (
                connection.sync_interval_minutes
                if sync_interval_minutes is None
                else sync_interval_minutes
            ),
        )
        connection.timeout_seconds = changed["timeout_seconds"] = timeout
        connection.sync_interval_minutes = changed["sync_interval_minutes"] = interval
    if allow_private_network is not None:
        check_url(connection.base_url, allow_private_network, resolve)
        connection.allow_private_network = changed["allow_private_network"] = allow_private_network
    if paused is not None:
        connection.paused = changed["paused"] = paused
    if token is not None:
        if not token.strip():
            raise ConnectionRefused("The token must not be empty")
        store_token(db, connection.id, token, _box(), actor)
        changed["token_replaced"] = True
    if changed:
        audit.record(
            db,
            actor=actor,
            action="connection.changed",
            resource_type="gitlab_instance",
            resource_id=str(connection.id),
            details={"url": connection.base_url, **changed},
        )
    return connection


def remove(db: Session, connection_id: int, actor: str) -> None:
    """Stop collecting a connection and forget its token; its data and evidence stay."""
    connection = db.get(GitLabInstance, connection_id)
    if connection is None or connection.removed_at is not None:
        raise LookupError(connection_id)
    if is_primary(connection, configured_settings()):
        raise ConnectionConflict(
            "The primary connection is disconnected on the Settings page or in the deployment"
        )
    connection.removed_at = db.execute(select(func.now())).scalar_one()
    forget_token(db, connection.id)
    audit.record(
        db,
        actor=actor,
        action="connection.removed",
        resource_type="gitlab_instance",
        resource_id=str(connection.id),
        details={"url": connection.base_url},
    )
