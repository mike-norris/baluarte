# Backup, restore and retention

## What to back up

All Baluarte state is in **one PostgreSQL database**: inventory (`gitlab_*`), collection jobs and runs, observations, evaluations, findings, policies, the audit trail (`audit_event`), users and sessions. The containers are stateless and the web image is static, so nothing else needs a backup.

Secrets are not in the database and must be kept in your secret store, separately from database backups:

| Secret | Used by | Losing it |
|---|---|---|
| Database password | migrate, API, worker | Reset it in PostgreSQL and the secret |
| OIDC client secret (if any) | API | Rotate at the identity provider |
| GitLab token | API, worker | Issue a new `read_api` token |

Backups contain inventory, evidence and audit history (user subjects, emails, GitLab paths). Encrypt them at rest, keep a copy off the database host, and restrict who can read or restore them as tightly as the database itself. They contain no GitLab token or OIDC secret, and session cookies are stored only as SHA-256 digests.

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

## Retention

**Nothing is deleted automatically yet.** Rows accumulate until an operator removes them:

| Data | Today |
|---|---|
| `audit_event` | Append-only: triggers reject `UPDATE` and `DELETE` |
| `policy` | Triggers reject `UPDATE` (migration `0005`): a saved version never changes; a new version is a new row |
| `observation`, `evaluation` | Triggers reject `UPDATE`; `DELETE` is allowed so retention can expire them. An observation is added only when a target's status, payload or error changes, so growth follows the rate of change in GitLab, not the number of runs. `observation.retention_until` exists but nothing sets or enforces it |
| `evaluation` growth | Unlike observations, a row is added for every rule and project on every evaluation run, so each check is on record: about rules × projects × runs per day (2 rules, 10,000 projects and 4 runs a day is 80,000 rows a day). Plan the retention period with this in mind |
| `finding` | Mutable current state; resolved findings are kept |
| `collection_job`, `collection_run`, `evaluation_run` | Kept indefinitely; the run history feeds freshness |
| `user_session`, `oidc_login` | Expire logically (idle, absolute and 10-minute limits) but rows remain |
| `gitlab_group`, `gitlab_project` | Kept; deleted or hidden resources become `missing`, not removed |

Not implemented: a retention period, an expiry job, legal hold, archival before deletion, and pruning of expired sessions or old jobs. The retention period is an open product question ([PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md)); each organization must decide it with its compliance owner, together with how long backups are kept, since a backup retains whatever a later deletion removes.
