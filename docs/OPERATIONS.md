# Backup, restore and retention

## What to back up

All Baluarte state is in **one PostgreSQL database**: inventory (`gitlab_*`), collection jobs and runs, observations, evaluations, findings, policies, the audit trail (`audit_event`), users and sessions, and the settings entered in the web UI. The containers are stateless and the web image is static, so nothing else needs a backup.

Keep these secrets in your secret store, separately from database backups:

| Secret | Used by | Losing it |
|---|---|---|
| Database password | migrate, API, worker | Reset it in PostgreSQL and the secret |
| Secret key (`BALUARTE_SECRET_KEY_FILE`) | API, worker | The client secret and GitLab token entered in the web UI can no longer be read: enter them again on the Settings page |
| OIDC client secret (if set by the deployment) | API | Rotate at the identity provider |
| GitLab token (if set by the deployment) | API, worker | Issue a new `read_api` token |

A client secret or GitLab token entered in the web UI is in the database, sealed with the secret key ([ADR-0008](architecture/ADR-0008-settings-in-the-database.md)). Restoring a backup needs the key that was in use when it was taken. Rotate the key as described in [SECURITY.md](SECURITY.md#setup-and-secrets-entered-in-the-web-ui).

Backups contain inventory, evidence and audit history (user subjects, emails, GitLab paths). Encrypt them at rest, keep a copy off the database host, and restrict who can read or restore them as tightly as the database itself. They contain no plaintext GitLab token or OIDC secret, and session cookies and the setup code are stored only as SHA-256 digests.

## Docker Compose

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

Thresholds: `failing` after 3 failed runs in a row for any kind; `overdue` when scheduled and a kind's last success is older than twice the interval. Scheduled jobs retry after 1, 5 and 15 minutes, so a short network or GitLab outage recovers without an alert ([GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md#scheduling-and-freshness)).

An auditor reads a gap in successful collection as a period in which the monitoring control did not operate. Keep the alert routed, and record the cause and fix of any outage.

## Retention

The operating profile ([COMPLIANCE.md](COMPLIANCE.md)) sets how long data is kept: **audit events** (`audit_retention_days`, default 365), **evidence** (`evidence_retention_days`, default 365) and **jobs** (`job_retention_days`, default 90). SOC 2, ISO 27001 and NIST 800-53 profiles require at least 365 days for audit events and evidence. The worker queues the `retention.expire` job once per UTC day; in one transaction it deletes:

| Data | Deleted when |
|---|---|
| `audit_event` | Older than the audit period, only through `baluarte_expire_audit` (below). Never younger than 30 days |
| `evaluation` | Older than the evidence period, unless it is a finding's latest evaluation |
| `observation` | Older than the evidence period, unless it is the newest of its target (instance, project or group, and source) or an evaluation still refers to it |
| `evaluation_run`, `collection_run` | Older than the evidence period and finished, unless an evaluation, observation or security policy still refers to it. Runs are evidence of collection failures, so they follow the evidence period, not the job period |
| `collection_job`, `export_job` | Finished more than the job period ago and not referred to by a run or observation. Queued and running jobs are never deleted |
| `user_session`, `oidc_login`, `setup_session` | Ended (expired or revoked) more than 30 days ago |

Everything else is kept: `policy`, `policy_change`, `policy_exception`, `remediation_plan`, `compliance_exception`, `compliance_attestation`, `finding`, inventory and settings. An observation is added only when a target's status, payload or error changes, so growth follows the rate of change in GitLab; an evaluation row is added for every rule and project on every run (2 rules, 10,000 projects and 4 runs a day is 80,000 rows a day).

**Audit events** cannot be deleted by the application: its database role has no `DELETE` on `audit_event` ([DEPLOYMENT.md](DEPLOYMENT.md#database-roles)), and a trigger rejects every `UPDATE` and `DELETE`. `baluarte_expire_audit(cutoff)` is a `SECURITY DEFINER` function owned by the schema owner; the trigger lets a delete through only while it runs. It refuses a cutoff less than 30 days ago and returns how many events it deleted, the first and last IDs, and a SHA-256 over their `details_digest` values in ID order. Each run is audited as `retention.expired` with the cutoffs, the counts per table and that digest, so an auditor holding an earlier export can confirm exactly which events were removed.

**Legal hold** (`legal_hold` in the profile) stops every deletion; the job still runs and records `retention.expired` with `legal_hold: true`. The posture's `retention-running` control is not met when the job has not succeeded for 48 hours.

Backups keep whatever a later deletion removes: decide how long backups are kept with the same compliance owner. Not implemented: archiving data before it is deleted, and per-record legal hold.
