"""Reading and writing the settings and sealed secrets entered through the web UI (ADR-0008).

Every secret written or read here is registered with the log redactor, so it cannot appear in a
log line even if a later error message would include it.
"""

from typing import Any

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from baluarte.auth.settings import AuthSettings, stored_auth_settings
from baluarte.configuration.crypto import Sealed, SecretBox, SecretUnavailable
from baluarte.db.models import StoredSecret, StoredSetting
from baluarte.integrations.gitlab.settings import (
    GitLabSettings,
    checked_interval,
    checked_timeout,
    gitlab_url,
)
from baluarte.logs import register_secret

OIDC = "oidc"
GITLAB = "gitlab"
# Names of stored secrets, not secret values.
OIDC_CLIENT_SECRET = "oidc_client_secret"  # noqa: S105
GITLAB_TOKEN = "gitlab_token"  # noqa: S105


def read_setting(db: Session, key: str) -> dict[str, Any] | None:
    row = db.get(StoredSetting, key)
    return dict(row.value) if row is not None else None


def write_setting(db: Session, key: str, value: dict[str, Any], actor: str) -> None:
    columns = {"value": value, "updated_by": actor, "updated_at": func.now()}
    db.execute(
        insert(StoredSetting)
        .values(key=key, **columns)
        .on_conflict_do_update(index_elements=["key"], set_=columns)
    )


def delete_setting(db: Session, key: str) -> None:
    db.execute(delete(StoredSetting).where(StoredSetting.key == key))


def secret_stored(db: Session, name: str) -> bool:
    return db.get(StoredSecret, name) is not None


def read_secret_value(db: Session, name: str, box: SecretBox | None) -> str | None:
    """Decrypt a stored secret; None when none is stored.

    Raises SecretUnavailable when one is stored but cannot be decrypted with the configured key.
    """
    row = db.get(StoredSecret, name)
    if row is None:
        return None
    if box is None:
        raise SecretUnavailable(
            f"Stored secret {name} cannot be decrypted: BALUARTE_SECRET_KEY_FILE is not set"
        )
    value = box.open(name, Sealed(row.key_id, bytes(row.nonce), bytes(row.ciphertext)))
    register_secret(value)
    return value


def write_secret(db: Session, name: str, value: str, box: SecretBox, actor: str) -> None:
    register_secret(value)
    sealed = box.seal(name, value)
    columns = {
        "key_id": sealed.key_id,
        "nonce": sealed.nonce,
        "ciphertext": sealed.ciphertext,
        "updated_by": actor,
        "updated_at": func.now(),
    }
    db.execute(
        insert(StoredSecret)
        .values(name=name, **columns)
        .on_conflict_do_update(index_elements=["name"], set_=columns)
    )


def delete_secret(db: Session, name: str) -> None:
    db.execute(delete(StoredSecret).where(StoredSecret.name == name))


def auth_from_value(value: dict[str, Any], client_secret: str | None) -> AuthSettings:
    return stored_auth_settings(
        public_url_value=str(value.get("public_url", "")),
        issuer=str(value.get("issuer", "")),
        client_id=str(value.get("client_id", "")),
        client_secret=client_secret,
        scopes=str(value.get("scopes", "")),
        roles_claim=str(value.get("roles_claim", "")),
        groups=value.get("groups") or {},
    )


def load_stored_auth(db: Session, box: SecretBox | None) -> AuthSettings | None:
    value = read_setting(db, OIDC)
    if value is None:
        return None
    return auth_from_value(value, read_secret_value(db, OIDC_CLIENT_SECRET, box))


def load_stored_gitlab(db: Session, box: SecretBox | None) -> GitLabSettings | None:
    value = read_setting(db, GITLAB)
    if value is None:
        return None
    token = read_secret_value(db, GITLAB_TOKEN, box)
    if token is None:
        raise SecretUnavailable("The stored GitLab settings have no token")
    return GitLabSettings(
        url=gitlab_url(str(value.get("url", "")), "url"),
        token=token,
        timeout_seconds=checked_timeout(float(value.get("timeout_seconds", 15)), "timeout_seconds"),
        sync_interval_minutes=checked_interval(
            int(value.get("sync_interval_minutes", 360)), "sync_interval_minutes"
        ),
        source="database",
    )
