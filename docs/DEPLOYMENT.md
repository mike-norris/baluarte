# Deployment specification

## Common contract

One set of versioned OCI images: `baluarte-web`, `baluarte-app` (API and worker commands). Configuration through environment variables and mounted secrets, or for the identity provider and GitLab connection, the web UI setup wizard ([ADR-0008](architecture/ADR-0008-settings-in-the-database.md)). The same images run in Docker Compose and Kubernetes.

## Database roles

Two PostgreSQL roles. `baluarte-migrate` runs as the schema owner (`BALUARTE_DATABASE_URL` and `BALUARTE_DATABASE_PASSWORD_FILE`). With `BALUARTE_APP_DATABASE_USER` and `BALUARTE_APP_DATABASE_PASSWORD_FILE` set, it then creates that login role, or updates its password, and grants it on every run:

- `SELECT`, `INSERT`, `UPDATE` and `DELETE` on the application tables;
- only `SELECT` and `INSERT` on `audit_event`, and only `SELECT` on `alembic_version`;
- no ownership, no `CREATE` on the schema and no `TRUNCATE`, so it cannot drop or disable the triggers that keep evidence append-only.

The API and worker connect as that role. The password is sent to PostgreSQL as a SCRAM-SHA-256 verifier, never in plain text. The migration user needs `CREATEROLE`; where a managed database does not allow that, create the role yourself (`CREATE ROLE baluarte_app LOGIN PASSWORD ...`, with no other attributes) and `baluarte-migrate` will only update its grants and password. A superuser, or the owner itself, is refused as the app role. After restoring a backup into a new server, run migrations again so the role and grants exist there. The Compose stack and the Helm chart both use `baluarte_app` by default (`database.app.*` in the chart).

`BALUARTE_BACKUP_DATABASE_USER` and `BALUARTE_BACKUP_DATABASE_PASSWORD_FILE` do the same for `baluarte_backup`, the role the backup service connects as. It gets `SELECT` on every table and sequence, which is what `pg_dump` needs, and `INSERT` on `backup_run`. Compose always creates it; the chart creates it when `backup.enabled` ([OPERATIONS.md](OPERATIONS.md#scheduled-backups)).

## Docker Compose

Web, API, worker, and PostgreSQL for development, plus an optional `backup` profile (`baluarte-backup` image). `BALUARTE_DEVELOPMENT_MODE=true` lets one person turn two-person review off while developing ([COMPLIANCE.md](COMPLIANCE.md#development-mode)); leave it unset anywhere the evidence matters. Document required secrets, TLS reverse proxy, volume backup, and safe upgrades. Do not ship production default credentials. Backup and restore: [OPERATIONS.md](OPERATIONS.md).

## Kubernetes

Maintained Helm chart with web/API/worker Deployments, Services, Ingress configuration, optional external PostgreSQL, Secrets references, ConfigMaps, resource requests/limits, non-root security contexts, probes, migration Job, and PodDisruptionBudget as appropriate. Namespace-scoped RBAC only where necessary. The chart's optional development database has no backups; production uses an external PostgreSQL with its own backups and point-in-time recovery ([OPERATIONS.md](OPERATIONS.md#kubernetes-helm)).

## Release validation

CI builds images, tests Docker Compose startup, and installs chart into a disposable Kubernetes cluster (e.g. kind) for smoke tests. Verify schema migrations, worker scheduling, health, and graceful shutdown. Support air-gapped image loading and configurable registries.
