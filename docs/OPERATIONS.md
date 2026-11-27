# Backup, restore and retention

## What to back up

All Baluarte state is in **one PostgreSQL database**: inventory (`gitlab_*`), collection jobs and runs, observations, evaluations, findings, policies, the audit trail (`audit_event`), users and sessions, and the settings entered in the web UI. The containers are stateless and the web image is static, so nothing else needs a backup.

Keep these secrets in your secret store, separately from database backups:

| Secret | Used by | Losing it |
|---|---|---|
| Database owner password | migrate | Reset it in PostgreSQL and the secret |
| App and backup role passwords | API and worker; backup service | Put a new value in the secret and re-run migrate, which resets the role's password |
| age private key for backups | nobody online: keep it offline | Backups encrypted to its public key cannot be read |
| Secret key (`BALUARTE_SECRET_KEY_FILE`) | API, worker | The client secret and GitLab token entered in the web UI can no longer be read: enter them again on the Settings page |
| OIDC client secret (if set by the deployment) | API | Rotate at the identity provider |
| GitLab token (if set by the deployment) | API, worker | Issue a new `read_api` token |
| Label write token (entered in the web UI, sealed) | Worker, applying label plans | Revoke it in GitLab and store a new one on the Settings page ([LABELS.md](LABELS.md#the-write-token)) |
| Remediation write token (entered in the web UI, sealed) | Worker, applying remediation plans | Revoke it in GitLab and store a new one on the Settings page ([REMEDIATION.md](REMEDIATION.md#the-write-token)) |

A client secret or GitLab token entered in the web UI is in the database, sealed with the secret key ([ADR-0008](architecture/ADR-0008-settings-in-the-database.md)). Restoring a backup needs the key that was in use when it was taken. Rotate the key as described in [SECURITY.md](SECURITY.md#setup-and-secrets-entered-in-the-web-ui).

Backups contain inventory, evidence and audit history (user subjects, emails, GitLab paths). Encrypt them at rest, keep a copy off the database host, and restrict who can read or restore them as tightly as the database itself. They contain no plaintext GitLab token or OIDC secret, and session cookies and the setup code are stored only as SHA-256 digests.

## Scheduled backups

The `baluarte-backup` image (`deploy/backup/`) does the following on a schedule:

1. Runs `pg_dump` as `baluarte_backup`, a role that can only read data and record its own runs.
2. Pipes the dump straight into [age](https://age-encryption.org) for one recipient (a public key). No unencrypted copy is ever written.
3. Writes `baluarte-<UTC timestamp>.dump.age` and keeps the newest `BALUARTE_BACKUP_KEEP` (14) files.
4. When an off-site target is configured, copies the encrypted file there and reads it back ([Off-site copies](#off-site-copies)).
5. Records each run, failed or not, in `backup_run` with its size, SHA-256 and off-site outcome.

The Compliance page reads those runs. Its `backups` control is met while the last successful run is within the profile's `backup_interval_hours` (24) plus 2 hours. It lists the runs and generates the configuration below.

Create the key pair once, somewhere safe. Only the public key goes to Baluarte:

```sh
age-keygen -o baluarte-backup.key     # prints "Public key: age1..."; store the file offline
```

**Docker Compose:**

```sh
mkdir -p backups && sudo chown 70:70 backups       # the image runs as uid 70 (Linux)
BALUARTE_BACKUP_RECIPIENT=age1... docker compose --profile backup up -d
```

The service backs up once at start, then every `BALUARTE_BACKUP_INTERVAL_HOURS`. Copy the files off the host with an [off-site target](#off-site-copies), whose result the `backup-offsite` check reads. If you copy them another way, such as a `BALUARTE_BACKUP_DIR` on a mounted network share, attest to it instead. `secrets/backup_database_password` is created with the other secrets (README).

**Kubernetes:** create a PersistentVolumeClaim for the backup files, then `--set backup.enabled=true --set backup.recipient=age1... --set backup.existingClaim=baluarte-backups`. This adds a CronJob, daily at 02:17 by default. The chart does not create or delete the claim, so backups outlive the release.

```sh
kubectl -n baluarte apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: baluarte-backups}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 20Gi}}}
YAML
```

With an external database, also set `backup.existingSecret` to a Secret holding a password for the `baluarte_backup` role, which the migration Job creates. A managed database's own backups can replace this; then attest to them.

### Off-site copies

A backup on the database host is lost with the host. Set an S3-compatible target and each new encrypted file is also copied there with [rclone](https://rclone.org). Only the encrypted file leaves the host, so the target never holds readable data. After copying, the service reads the object back and checks its size and the SHA-256 stored with it. The run records `copied` (with `s3://bucket/key` and the endpoint's host, never the credentials) or `failed` with the reason, and the Compliance page shows it for each run. A failed copy fails the run's health check, so it is noticed.

| Variable | Default | Meaning |
|---|---|---|
| `BALUARTE_BACKUP_S3_ENDPOINT` | (off) | `https://…`; `http://` only on a private network |
| `BALUARTE_BACKUP_S3_BUCKET` | | The bucket |
| `BALUARTE_BACKUP_S3_PREFIX` | `baluarte/` | Key prefix inside the bucket |
| `BALUARTE_BACKUP_S3_REGION` | `us-east-1` | Region, for targets that check it |
| `BALUARTE_BACKUP_S3_KEEP` | the local keep | Copies kept; older ones are deleted. `0` never deletes |
| `BALUARTE_BACKUP_S3_ACCESS_KEY_ID_FILE`, `_SECRET_ACCESS_KEY_FILE` | | Files holding the credentials |

**Choosing the target.** Any S3-compatible service works: a cloud bucket, or the S3 service of a NAS. The best target is in another site or account. It has versioning or object lock, and its backup credentials can write but not delete, with `BALUARTE_BACKUP_S3_KEEP=0` and the target's own lifecycle rules removing old copies.

**A local target for Docker Compose.** `deploy/compose/offsite.yaml` adds a [SeaweedFS](https://github.com/seaweedfs/seaweedfs) container (Apache 2.0) that serves the S3 API to the backup service only, on the Compose network:

```sh
deploy/compose/make-offsite-credentials.sh        # S3 keys, allowed only the backup bucket
BALUARTE_OFFSITE_DIR=/mnt/nas/baluarte-offsite \
BALUARTE_BACKUP_RECIPIENT=age1... \
docker compose -f compose.yaml -f deploy/compose/offsite.yaml --profile backup up -d
```

The container only provides the API. **What makes the copies off site is the storage behind `BALUARTE_OFFSITE_DIR`.** It must be a mount of storage that is not the host's own disk, such as a NAS share, a SAN or network block device, or a separately attached volume, so that losing the host does not lose the copies. The default `./offsite` directory is for trying it out only.

`deploy/compose/offsite-test.sh` checks the whole path and runs in CI:

- it backs up, copies, keeps the newest copies and downloads one, which must match the recorded SHA-256 and decrypt with the offline key into a restorable dump;
- it runs a [restore test](#restore-tests) from the off-site copy, [lists](#listing-backups) the backups and [restores](#restoring) into an empty database;
- it confirms that a replaced off-site copy and a rewritten audit row both fail the restore test.

**Kubernetes:** set `backup.offsite.endpoint`, `backup.offsite.bucket` and `backup.offsite.existingSecret`, a Secret with `access-key-id` and `secret-access-key`.

### Listing backups

`baluarte-backup list` compares the recorded backups with what is stored. It shows one line per backup, newest first. Each line says whether the local file and the off-site copy are present and match the recorded size, and the SHA-256 for the off-site copy (`ok`, `missing` or `DIFFERS`). Files with no record are listed too.

```sh
docker compose --profile backup run --rm backup list
```

### Restore tests

`baluarte-backup restore-test [FILE]` proves that a backup can be restored. It needs the age private key, mounted for that run only, so run it from the machine that keeps the key:

```sh
docker compose --profile backup run --rm \
  -v "$PWD/baluarte-backup.key:/run/secrets/restore_identity:ro" backup restore-test
```

It takes the newest successful backup, or `FILE`, and:

1. Fetches the off-site copy if the backup was copied there, otherwise the local file. An off-site copy that cannot be fetched fails the test rather than falling back.
2. Checks its SHA-256 against `backup_run`.
3. Decrypts it straight into a temporary PostgreSQL 17 inside the container (`BALUARTE_RESTORE_DIR`, `/tmp/baluarte-restore`, a tmpfs under Compose). It listens on a Unix socket only. No decrypted dump is written.
4. Checks the alembic revision, the tables, that `audit_event` has its immutability triggers, and every audit event's `details_digest`, recomputed the way the application does.
5. Deletes the temporary database and records a `restore_run`: the backup, where it came from and what was checked, or why it failed.

Compliance > Backups lists the restore tests. The `restore-test` control is met while the latest restore test in the last 365 days succeeded. A recorded failure is not met, even with an attestation. Restore tests you run another way can still be attested.

**Kubernetes:** `backup.restoreTest.enabled` adds a monthly CronJob. It needs the private key in a Secret (`backup.restoreTest.existingSecret`), so anyone who can read that Secret can decrypt every backup. If that is not acceptable, run the image from a trusted machine with the same environment as the backup CronJob.

### Restoring

`baluarte-backup restore FILE [SHA256]` is the disaster recovery path. It restores into `BALUARTE_RESTORE_TARGET`, a libpq connection string with its password in `BALUARTE_RESTORE_TARGET_PASSWORD_FILE`, and it refuses a database that already holds tables. It does not need the live database. The expected SHA-256 comes from the argument, from `backup_run` if the old database still answers, or from the off-site copy's metadata. Without one it refuses, so an unverified file is never restored. The file is decrypted straight into `pg_restore`.

```sh
docker compose --profile backup run --rm \
  -v "$PWD/baluarte-backup.key:/run/secrets/restore_identity:ro" \
  -v "$PWD/secrets/postgres_password:/run/secrets/target_password:ro" \
  -e BALUARTE_RESTORE_TARGET="host=db dbname=baluarte user=baluarte" \
  -e BALUARTE_RESTORE_TARGET_PASSWORD_FILE=/run/secrets/target_password \
  backup restore baluarte-<timestamp>.dump.age
```

Then run the `migrate` service so roles and grants are set again, and follow [Verifying a restore](#verifying-a-restore). To decrypt a backup by hand instead:

```sh
age -d -i baluarte-backup.key baluarte-<timestamp>.dump.age > baluarte-<timestamp>.dump
```

## Manual backup and restore (Docker Compose)

The Compose stack is for development (see [DEPLOYMENT.md](DEPLOYMENT.md)); the same procedure applies to any PostgreSQL 17 database. `pg_dump` takes a consistent snapshot while Baluarte keeps running.

```sh
# Back up (custom format, compressed).
docker compose exec -T db pg_dump -U baluarte -d baluarte -Fc > baluarte-$(date -u +%Y%m%dT%H%M%SZ).dump

# Restore into an empty database: stop writers first so no job or sign-in lands mid-restore.
docker compose stop api worker web
docker compose exec -T db psql -U baluarte -d postgres -c 'DROP DATABASE baluarte WITH (FORCE)' -c 'CREATE DATABASE baluarte OWNER baluarte'
docker compose exec -T db pg_restore -U baluarte -d baluarte --no-owner --exit-on-error < baluarte-<timestamp>.dump
docker compose up -d --wait        # migrate re-runs and applies any newer migrations
```

Restores only move forward: `baluarte-migrate` upgrades an older dump to the current schema, but a dump from a newer Baluarte cannot be restored under an older one.

## Kubernetes (Helm)

`devDatabase.enabled=true` is a single PostgreSQL pod for evaluation and CI, with **no backups**; its data is lost with its PersistentVolumeClaim. For anything else, point the chart at a managed PostgreSQL or an operator-run cluster with point-in-time recovery (`database.host`, `database.existingSecret`, `database.sslMode`; see the [chart README](../deploy/helm/baluarte/README.md)) and use that service's backups. Logical `pg_dump`/`pg_restore` as above also works against it.

To restore, scale the API and worker to zero, restore the database with your provider's tooling, then `helm upgrade` (every revision runs a new `baluarte-migrate` Job, and the API stays unready until the schema is current) and scale back up.

## Verifying a restore

1. **Schema:** `baluarte-migrate` exits 0 (the migrate service or Job), and `GET /api/v1/ready` answers `{"status": "ready"}`.
2. **Audit digests:** recompute every `audit_event.details_digest` (SHA-256 of `details` as canonical JSON: sorted keys, separators `,` and `:`, UTF-8, non-ASCII kept). PostgreSQL's `jsonb` text form adds spaces, so recompute in Python with the app's own function:

   ```sh
   docker compose run --rm -T --no-deps migrate python - <<'PY'
   from sqlalchemy import select
   from sqlalchemy.orm import Session
   from baluarte.audit import details_digest
   from baluarte.db.models import AuditEvent
   from baluarte.db.session import get_engine
   with Session(get_engine()) as db:
       bad = [e.id for e in db.scalars(select(AuditEvent)) if details_digest(e.details) != e.details_digest]
   print("mismatched audit events:", bad or "none")
   PY
   ```
   Any mismatch means the row changed after it was written. A match does not prove the row is original (see [audit limitations](AUDIT_EVIDENCE.md#audit-limitations)).
3. **Triggers:** an `UPDATE` on `audit_event` is rejected with "rows are immutable evidence".
4. **Sessions:** after restoring to an earlier point, revoke all sessions so nobody keeps access that was revoked after the backup was taken: `UPDATE user_session SET revoked_at = now() WHERE revoked_at IS NULL;` Users sign in again.
5. **Freshness:** inventory and runs are as old as the backup. `GET /api/v1/gitlab/freshness` shows that, and the next scheduled collection (or `POST /api/v1/gitlab/groups/discover` and `/projects/discover`) brings them current.

`deploy/compose/backup-restore-test.sh` runs this procedure end to end against throwaway containers. It migrates a database, writes audit events, dumps and restores them, re-runs migrations, checks the revision, the triggers and every digest, and confirms that the digest check detects a rewritten row. It publishes no ports and uses its own network and image tag.

## Monitoring collection

A failed collection is recorded, but nobody is told unless something watches for it. Baluarte gives two signals:

- **Logs:** the worker logs `event: gitlab.collection_unhealthy` (WARNING) once when collection starts failing or falls overdue, and `event: gitlab.collection_recovered` (WARNING) once when it recovers. Route the worker's JSON log to your log platform and alert on the first event. Each worker process logs its own transitions, so with several workers expect one event per worker.
- **API:** `GET /api/v1/gitlab/freshness` returns `alert` (null while healthy). A probe can alert while it is non-null.
- **Notifications:** with the collection health event on ([NOTIFICATIONS.md](NOTIFICATIONS.md)), one email or chat message when collection becomes unhealthy and one when it recovers, however many workers run.

Thresholds: `failing` after 3 failed runs in a row for any kind; `overdue` when scheduled and a kind's last success is older than twice the interval. Scheduled jobs retry after 1, 5 and 15 minutes, so a short network or GitLab outage recovers without an alert ([GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#scheduling-and-freshness)).

An auditor reads a gap in successful collection as a period in which the monitoring control did not operate. Keep the alert routed, and record the cause and fix of any outage.

## Notifications

The worker sends [notifications](NOTIFICATIONS.md) once a channel is configured under Settings → Notifications. To check them:

- Settings → Notifications lists the latest 50 deliveries with their status and last error; `GET /api/v1/notifications/deliveries` returns the same to administrators and auditors.
- The worker logs `event: notifications.delivery_failed` (WARNING) for each failed attempt, with the channel and a redacted reason. Attempts are retried after 1, 5, 15 and 60 minutes; the fifth failure marks the delivery `failed` and it is not sent again.
- "Send test" on each channel says which stage failed: connect (DNS, firewall, port), TLS (certificate not trusted, or no STARTTLS), authentication, or the server rejected the message (recipients, sender, size).

Notifications need outbound access from the worker to the SMTP server and webhook host only. No new environment variables are needed; the secret key (`BALUARTE_SECRET_KEY_FILE`) must be mounted in the API (to save secrets) and the worker (to send).

## Retention

The operating profile ([COMPLIANCE.md](COMPLIANCE.md)) sets how long data is kept: **audit events** (`audit_retention_days`, default 365), **evidence** (`evidence_retention_days`, default 365) and **jobs** (`job_retention_days`, default 90). SOC 2, ISO 27001 and NIST 800-53 profiles require at least 365 days for audit events and evidence. The worker queues the `retention.expire` job once per UTC day; in one transaction it deletes:

| Data | Deleted when |
|---|---|
| `audit_event` | Older than the audit period, only through `baluarte_expire_audit` (below). Never younger than 30 days |
| `evaluation` | Older than the evidence period, unless it is a finding's latest evaluation |
| `change_record` | Landed on its branch before the evidence period, unless an evaluation still refers to its observation |
| `time_entry`, `issue_estimate`, `issue_context` | Spent (time entries) or last read (estimates, issue context) before the time tracking `retention_days` when that is shorter than the evidence period, else before the evidence period ([TIME_TRACKING.md](TIME_TRACKING.md#settings-and-retention)) |
| `observation` | Older than the evidence period, unless it is the newest of its target (instance, project or group, and source), or an evaluation (`observation_id` or `observation_ids`), a change, a time entry or an issue estimate still refers to it |
| `evaluation_run`, `collection_run` | Older than the evidence period and finished, unless an evaluation, observation or security policy still refers to it. Runs are evidence of collection failures, so they follow the evidence period, not the job period |
| `collection_job`, `export_job` | Finished more than the job period ago and not referred to by a run or observation. Queued and running jobs are never deleted |
| `user_session`, `oidc_login`, `setup_session` | Ended (expired or revoked) more than 30 days ago |
| `notification`, `notification_delivery` | Created before the audit period, unless a delivery is still pending; deliveries go with their notification ([NOTIFICATIONS.md](NOTIFICATIONS.md)) |
| Evidence item files | The file of an accepted or rejected evidence item whose evidence ended before the evidence period, unless an assessment still in force cites it; the item and the file's SHA-256 stay. Observations and changes an assessment cites are kept. The stored `captured_file` goes only through `baluarte_expire_captured_file`, and only when no other item or observation cites it ([EVIDENCE_MANAGEMENT.md](EVIDENCE_MANAGEMENT.md#evidence-items-m152)) |

Everything else is kept: `manager_scope`, `tag`, `tag_assignment`, `time_cursor`, `policy`, `policy_change`, `control_mapping_change`, `policy_exception`, `remediation_plan`, `compliance_exception`, `compliance_attestation`, `finding`, inventory and settings. An observation is added only when a target's status, payload or error changes, so growth follows the rate of change in GitLab; an evaluation row is added for every rule and project on every run (2 rules, 10,000 projects and 4 runs a day is 80,000 rows a day).

**Audit events** cannot be deleted by the application: its database role has no `DELETE` on `audit_event` ([DEPLOYMENT.md](DEPLOYMENT.md#database-roles)), and a trigger rejects every `UPDATE` and `DELETE`. `baluarte_expire_audit(cutoff)` is a `SECURITY DEFINER` function owned by the schema owner; the trigger lets a delete through only while it runs. It refuses a cutoff less than 30 days ago and returns how many events it deleted, the first and last IDs, and a SHA-256 over their `details_digest` values in ID order. Each run is audited as `retention.expired` with the cutoffs, the counts per table and that digest, so an auditor holding an earlier export can confirm exactly which events were removed.

**Legal hold** (`legal_hold` in the profile) stops every deletion; the job still runs and records `retention.expired` with `legal_hold: true`. The posture's `retention-running` control is not met when the job has not succeeded for 48 hours.

Backups keep whatever a later deletion removes: decide how long backups are kept with the same compliance owner. Not implemented: archiving data before it is deleted, and per-record legal hold.
