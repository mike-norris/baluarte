"""Backups as Baluarte sees them: the runs the ``baluarte-backup`` service reports in
``backup_run``, and the configuration it should run with.

The service runs ``pg_dump`` as its own read-only database role, encrypts the dump to an age
public key, and records every run, failed or not. Baluarte never holds the private key, so it
cannot read a backup; it only checks that one was taken and encrypted in time.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.compliance import operations
from baluarte.db.models import BackupRun

RECENT_RUNS = 20
# How long after the interval a backup may finish before it counts as missed.
GRACE = timedelta(hours=2)
KEEP = 14
RECIPIENT_PLACEHOLDER = "age1replace-with-your-backup-public-key"
DOCS = "docs/OPERATIONS.md#scheduled-backups"


@dataclass(frozen=True)
class Backups:
    interval_hours: int
    last_success: BackupRun | None
    runs: list[BackupRun]


def interval_hours(db: Session) -> int:
    return int(operations.load(db).settings["backup_interval_hours"])


def last_success(db: Session) -> BackupRun | None:
    return db.scalars(
        select(BackupRun)
        .where(BackupRun.status == "succeeded", BackupRun.encrypted.is_(True))
        .order_by(BackupRun.finished_at.desc(), BackupRun.id.desc())
        .limit(1)
    ).one_or_none()


def recent(db: Session, limit: int = RECENT_RUNS) -> list[BackupRun]:
    return list(
        db.scalars(
            select(BackupRun)
            .order_by(BackupRun.finished_at.desc(), BackupRun.id.desc())
            .limit(limit)
        )
    )


def overview(db: Session) -> Backups:
    return Backups(interval_hours(db), last_success(db), recent(db))


def due_by(run: BackupRun, hours: int) -> datetime:
    """When the next backup after ``run`` must have finished."""
    return run.finished_at + timedelta(hours=hours) + GRACE


# Configuration -------------------------------------------------------------------------------


def schedule(hours: int) -> str:
    """A cron schedule that runs at least every ``hours`` hours."""
    if hours >= 48:
        return f"17 2 */{hours // 24} * *"
    if hours >= 24:
        return "17 2 * * *"
    return f"17 */{hours} * * *"


def compose_env(hours: int) -> str:
    return (
        "# Baluarte backup service: add to the .env file next to compose.yaml.\n"
        "# The age public key the backups are encrypted to (from age-keygen).\n"
        f"BALUARTE_BACKUP_RECIPIENT={RECIPIENT_PLACEHOLDER}\n"
        "# Hours between backups; the operating profile requires at most this.\n"
        f"BALUARTE_BACKUP_INTERVAL_HOURS={hours}\n"
        "# How many backup files to keep.\n"
        f"BALUARTE_BACKUP_KEEP={KEEP}\n"
        "# Where the files are written; must be writable by uid 70.\n"
        "BALUARTE_BACKUP_DIR=./backups\n"
    )


def helm_values(hours: int) -> str:
    return (
        "# Baluarte backup CronJob: pass with helm upgrade -f backup-values.yaml.\n"
        "backup:\n"
        "  enabled: true\n"
        "  # The age public key the backups are encrypted to (from age-keygen).\n"
        f"  recipient: {RECIPIENT_PLACEHOLDER}\n"
        f"  # At least every {hours} hours, as the operating profile requires.\n"
        f'  schedule: "{schedule(hours)}"\n'
        f"  keep: {KEEP}\n"
        "  user: baluarte_backup\n"
        "  # Required with an external database: a Secret holding the backup role's password.\n"
        "  # existingSecret: baluarte-backup\n"
        "  # passwordKey: password\n"
        "  persistence:\n"
        "    size: 20Gi\n"
        "    # storageClassName: \n"
        "    # existingClaim: \n"
    )


def compose_how_to(hours: int) -> str:
    return f"""Run the `baluarte-backup` service with Docker Compose. It backs up at start, then \
every `BALUARTE_BACKUP_INTERVAL_HOURS` hours ({hours} for this profile).

1. Create the key pair on a machine you trust and keep the private key **offline**. Only the \
public key (`age1...`) goes on the server; without the private key nobody, including \
Baluarte, can read a backup.

   ```sh
   age-keygen -o baluarte-backup.key
   ```

   The public key is printed and is also on the `# public key:` line of the file.

2. Create the backup role's password, unless the README's setup already did. \
`baluarte-migrate` creates the `baluarte_backup` role from it, with read access only. The file \
must be readable inside the container, as the other files in `secrets/` are.

   ```sh
   [ -f secrets/backup_database_password ] || \
     (umask 022 && openssl rand -base64 32 > secrets/backup_database_password)
   ```

3. Create the backup directory, writable by the service (uid 70).

   ```sh
   mkdir -p backups && sudo chown 70:70 backups
   ```

4. Add the downloaded `.env` lines, with your public key, and start the service. Migrate \
runs first and creates the role.

   ```sh
   BALUARTE_BACKUP_RECIPIENT=age1... docker compose --profile backup up -d
   ```

5. Copy the `backups` directory off this host (see **Backups are copied off the database \
host**), and test a restore at least once a year.

See {DOCS}.
"""


def helm_how_to(hours: int) -> str:
    return f"""Run the backup as a Kubernetes CronJob, on the schedule \
`{schedule(hours)}` (at least every {hours} hours).

1. Create the key pair on a machine you trust and keep the private key **offline**. Only the \
public key (`age1...`) goes in the values.

   ```sh
   age-keygen -o baluarte-backup.key
   ```

2. With an external database, create the `baluarte_backup` role's password as a Secret. \
The chart's development database generates one.

   ```sh
   kubectl create secret generic baluarte-backup \\
     --from-literal=password="$(openssl rand -base64 32)"
   ```

3. Enable the backup with the downloaded values file, or with `--set`.

   ```sh
   helm upgrade baluarte deploy/helm/baluarte --reuse-values \\
     --set backup.enabled=true \\
     --set backup.recipient=age1... \\
     --set backup.existingSecret=baluarte-backup --set backup.passwordKey=password
   ```

4. Copy the backup volume off the cluster's storage, and test a restore at least once a year.

See {DOCS}.
"""
