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

Web, API, worker, and PostgreSQL for development, plus an optional `backup` profile (`baluarte-backup` image). `BALUARTE_DEVELOPMENT_MODE=true` lets one person turn two-person review off while developing ([COMPLIANCE.md](COMPLIANCE.md#development-mode)); leave it unset anywhere the evidence matters. `BALUARTE_LABEL_WRITES=true` lets approved label plans be applied with the write token entered on the Settings page ([LABELS.md](LABELS.md)); it defaults to `false`, which keeps Baluarte read-only. `BALUARTE_ISSUE_WRITES=true` lets approved issue plans, the time rules on closed issues, be applied with the same token ([TIME_TRACKING.md](TIME_TRACKING.md)). `BALUARTE_OIDC_MANAGER_GROUPS` maps identity provider groups to the manager role ([MANAGER_SPACE.md](MANAGER_SPACE.md)). `BALUARTE_TIME_PER_PERSON` (default `true`) can be set to `false` to turn off time per person for everyone, for example until a works council or privacy officer has agreed to it ([TIME_TRACKING.md](TIME_TRACKING.md)). Document required secrets, TLS reverse proxy, volume backup, and safe upgrades. Do not ship production default credentials. Backup and restore: [OPERATIONS.md](OPERATIONS.md).

## Kubernetes

Maintained Helm chart with web/API/worker Deployments, Services, Ingress configuration, optional external PostgreSQL, Secrets references, ConfigMaps, resource requests/limits, non-root security contexts, probes, migration Job, and PodDisruptionBudget as appropriate. Namespace-scoped RBAC only where necessary. `gitlab.labelWrites` (default `false`) sets `BALUARTE_LABEL_WRITES` and `BALUARTE_ISSUE_WRITES` for the API and worker. `auth.oidc.managerGroups` sets `BALUARTE_OIDC_MANAGER_GROUPS`, and `timeTracking.perPerson` (default `true`) sets `BALUARTE_TIME_PER_PERSON` for the API and worker. The chart's optional development database has no backups; production uses an external PostgreSQL with its own backups and point-in-time recovery ([OPERATIONS.md](OPERATIONS.md#kubernetes-helm)).

## Releases

A release is a tag `vX.Y.Z` on `main`; `.github/workflows/release.yml` publishes it. The tag must match the version in `backend/pyproject.toml`, `frontend/package.json` and `deploy/helm/baluarte/Chart.yaml`, and `CHANGELOG.md` must have a section for it. A tag with a pre-release suffix (`v0.2.0-rc.1`) is published as a pre-release with only its exact image tags.

| Artifact | Where |
|---|---|
| Images (linux/amd64, linux/arm64) | `ghcr.io/mike-norris/baluarte-app`, `ghcr.io/mike-norris/baluarte-web`, `ghcr.io/mike-norris/baluarte-backup`, tagged `X.Y.Z` and `X.Y` |
| Helm chart | `oci://ghcr.io/mike-norris/charts/baluarte`, version `X.Y.Z` |
| Release notes and the packaged chart with `SHA256SUMS` | the GitHub release for the tag |

Each image is scanned with Trivy (fixable HIGH and CRITICAL vulnerabilities fail the release) before it is pushed, carries an SBOM and build provenance, and is signed with cosign using the release workflow's identity, as is the chart. Pin images by version or digest, never by a moving tag. To verify a signature:

```sh
cosign verify ghcr.io/mike-norris/baluarte-app:0.1.0 \
  --certificate-identity-regexp '^https://github.com/mike-norris/baluarte/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Install the chart with `helm install baluarte oci://ghcr.io/mike-norris/charts/baluarte --version 0.1.0 -f values.yaml`; it pulls the images of the same version from `ghcr.io/mike-norris` (`global.imageRegistry`, which a mirror or air-gapped registry overrides). To run the Compose stack from released images instead of building them, set `BALUARTE_IMAGE_PREFIX=ghcr.io/mike-norris/` and `BALUARTE_VERSION=0.1.0`, then `docker compose pull` and `docker compose up -d --no-build --wait`.

## Production

See [HARDENING.md](HARDENING.md) for what the images and chart already enforce and the checklist for operators.

## Release validation

CI builds images, tests Docker Compose startup, and installs chart into a disposable Kubernetes cluster (e.g. kind) for smoke tests. Verify schema migrations, worker scheduling, health, and graceful shutdown. Support air-gapped image loading and configurable registries.
