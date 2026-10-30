"""Command-line tools for setup and stored secrets, run inside the API or worker container.

``baluarte-setup-code`` prints a new one-time setup code and ends every setup session, for when
the code in the API log is lost or may have been seen by someone else.

``baluarte-reencrypt-secrets`` seals every stored secret under the current key, after rotating
``BALUARTE_SECRET_KEY_FILE`` with the old key in ``BALUARTE_SECRET_KEY_PREVIOUS_FILE``.
"""

import argparse
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.config import database_url
from baluarte.configuration.crypto import Sealed, load_secret_box
from baluarte.configuration.setup import issue_code
from baluarte.db.models import StoredSecret
from baluarte.settings import ConfigurationError, configure_logging_from_env

ACTOR = "cli"


def setup_code(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="baluarte-setup-code",
        description="Issue a new one-time setup code. The previous code stops working and "
        "every setup session ends.",
    ).parse_args(argv)
    configure_logging_from_env()
    try:
        engine = create_engine(database_url(), connect_args={"connect_timeout": 5})
        with Session(engine) as db:
            code = issue_code(db, revoke_sessions=True)
            if code is not None:
                audit.record(db, actor=ACTOR, action="setup.code_issued", resource_type="setup")
            db.commit()
        engine.dispose()
    except ConfigurationError as exc:
        print(f"baluarte-setup-code: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"baluarte-setup-code: database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    if code is None:
        print("baluarte-setup-code: setup is already complete", file=sys.stderr)
        return 1
    print(code)
    return 0


def reencrypt_secrets(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="baluarte-reencrypt-secrets",
        description="Seal every stored secret under the key in BALUARTE_SECRET_KEY_FILE.",
    ).parse_args(argv)
    configure_logging_from_env()
    try:
        box = load_secret_box()
        if box is None:
            raise ConfigurationError("BALUARTE_SECRET_KEY_FILE is not set")
        engine = create_engine(database_url(), connect_args={"connect_timeout": 5})
        with Session(engine) as db:
            rows = db.scalars(select(StoredSecret).with_for_update()).all()
            changed = []
            for row in rows:
                if row.key_id == box.key_id:
                    continue
                value = box.open(
                    row.name, Sealed(row.key_id, bytes(row.nonce), bytes(row.ciphertext))
                )
                sealed = box.seal(row.name, value)
                row.key_id, row.nonce, row.ciphertext = (
                    sealed.key_id,
                    sealed.nonce,
                    sealed.ciphertext,
                )
                row.updated_by = ACTOR
                changed.append(row.name)
            if changed:
                audit.record(
                    db,
                    actor=ACTOR,
                    action="settings.secrets_reencrypted",
                    resource_type="settings",
                    details={"secrets": sorted(changed), "key_id": box.key_id},
                )
            db.commit()
        engine.dispose()
    except ConfigurationError as exc:
        print(f"baluarte-reencrypt-secrets: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"baluarte-reencrypt-secrets: database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"Re-encrypted {len(changed)} of {len(rows)} stored secrets under key {box.key_id}")
    return 0
