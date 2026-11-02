# Baluarte Helm chart

Deploys the Baluarte API, background worker and web UI to Kubernetes 1.29+. The web pod serves the UI and proxies `/api/` to the API Service, so an Ingress only needs to route to the web Service.

Every install and upgrade runs a `baluarte-migrate` Job; the API reports ready only once the schema is current.

## Install

Images are not published yet; build them and push to a registry your cluster can pull from.

Use an external PostgreSQL in production. Create Secrets holding the owner's password and a new password for the app role (`baluarte_app`, which the migration Job creates), then:

```sh
kubectl -n baluarte create secret generic baluarte-database --from-file=password=./db-password
kubectl -n baluarte create secret generic baluarte-app-database --from-file=password=./app-db-password
helm install baluarte deploy/helm/baluarte \
  --namespace baluarte --create-namespace \
  --set global.imageRegistry=registry.example.internal/baluarte \
  --set database.host=postgresql.example.internal \
  --set database.existingSecret=baluarte-database \
  --set database.app.existingSecret=baluarte-app-database \
  --set ingress.enabled=true --set ingress.host=baluarte.example.internal
helm test baluarte -n baluarte
```

Then open the web UI. A new installation shows a setup wizard where an administrator enters the identity provider and, optionally, the GitLab connection. The wizard is unlocked by a one-time setup code that the API logs at startup; to read it, or to issue a new one:

```sh
kubectl -n baluarte logs deploy/baluarte-api | grep 'setup code'
kubectl -n baluarte exec deploy/baluarte-api -- baluarte-setup-code
```

The API Deployment is named `<release>-baluarte-api`, or `<release>-api` when the release name already contains `baluarte` (as here). Setup completes when the first user in an admin group signs in. Secrets entered in the UI are sealed with a key the chart generates (see `secretKey` below); back that Secret up with the database.

To configure sign-in in the chart instead, register a client with your identity provider (redirect URI `https://baluarte.example.internal/api/v1/auth/callback`), store its secret, and upgrade. Settings from the chart take precedence and are read-only in the UI:

```sh
kubectl -n baluarte create secret generic baluarte-oidc --from-file=client-secret=./oidc-client-secret
helm upgrade baluarte deploy/helm/baluarte -n baluarte --reuse-values \
  --set auth.oidc.issuer=https://idp.example.internal/realms/main \
  --set auth.oidc.clientId=baluarte \
  --set auth.oidc.existingSecret=baluarte-oidc \
  --set 'auth.oidc.adminGroups={baluarte-admins}'
```

For evaluation only, `--set devDatabase.enabled=true` runs a single PostgreSQL pod with a generated password. It has no backups or high availability. Back up the external database with your provider's tooling; restore and verification steps are in [docs/OPERATIONS.md](../../../docs/OPERATIONS.md#kubernetes-helm).

## Key values

| Value | Default | Purpose |
|---|---|---|
| `global.imageRegistry` | `""` | Registry prefix for both images (private or air-gapped registries) |
| `api.image.tag`, `web.image.tag` | chart `appVersion` | Image tags |
| `api.replicaCount`, `web.replicaCount`, `worker.replicaCount` | `1` | Replicas. Workers coordinate through PostgreSQL leases, so several can run safely |
| `worker.pollSeconds`, `worker.leaseSeconds` | `5`, `300` | Idle poll interval; how long a silent worker keeps a job before it is retried elsewhere |
| `worker.terminationGracePeriodSeconds` | `60` | Time to finish the current job after SIGTERM |
| `api.resources`, `web.resources` | small requests, memory limits | Resource requests and limits |
| `api.podDisruptionBudget.enabled`, `web.podDisruptionBudget.enabled`, `worker.podDisruptionBudget.enabled` | `false` | PodDisruptionBudgets (enable with 2+ replicas) |
| `database.host`, `database.port`, `database.name`, `database.user` | `""`, `5432`, `baluarte`, `baluarte` | External PostgreSQL; `host` is required unless `devDatabase.enabled` |
| `database.existingSecret`, `database.passwordKey` | `""`, `password` | Secret holding the database password |
| `database.sslMode` | `require` | libpq `sslmode`; use `verify-full` with a trusted CA where possible |
| `database.app.user`, `database.app.existingSecret`, `database.app.passwordKey` | `baluarte_app`, `""`, `password` | Role the API and worker connect as. The migration Job creates or updates it with data access only (no schema changes, no altering audit events), so the migration user needs `CREATEROLE`. `existingSecret` holds its password and is required with an external database; the dev database generates one. `user: ""` connects as the owner (not recommended) |
| `logging.level`, `logging.format` | `INFO`, `json` | Log level and format for the API, worker and migration Job; secrets are redacted |
| `migrations.waitTimeoutSeconds` | `120` | How long the migration Job waits for PostgreSQL |
| `devDatabase.enabled` | `false` | Single-pod PostgreSQL for evaluation and CI |
| `secretKey.existingSecret`, `secretKey.key` | `""`, `secret-key` | Key that seals secrets entered in the web UI (32 random bytes, base64). Empty generates `<fullname>-secret-key` on install, kept across upgrades and on uninstall |
| `auth.oidc.issuer`, `auth.oidc.clientId` | `""` | OIDC provider and client. Empty issuer leaves sign-in to the setup wizard; until it is set up, protected endpoints answer 503 |
| `auth.publicUrl` | `https://<ingress.host>` | Browser-facing https origin; register `<publicUrl>/api/v1/auth/callback` with the provider |
| `auth.oidc.existingSecret`, `auth.oidc.clientSecretKey` | `""`, `client-secret` | Secret holding the client secret; empty for a PKCE-only public client |
| `auth.oidc.adminGroups`, `analystGroups`, `auditorGroups` | `[]` | Provider groups granted each role; at least one is required |
| `auth.oidc.rolesClaim`, `auth.oidc.scopes` | `groups`, `openid profile email` | Groups claim and requested scopes |
| `backup.enabled`, `backup.recipient`, `backup.schedule`, `backup.keep` | `false`, `""`, `17 2 * * *`, `14` | Encrypted `pg_dump` CronJob ([OPERATIONS.md](../../../docs/OPERATIONS.md#scheduled-backups)). `recipient` is an age public key and is required when enabled |
| `backup.user`, `backup.existingSecret`, `backup.passwordKey` | `baluarte_backup`, `""`, `password` | Read-only role the migration Job creates for backups; `existingSecret` holds its password and is required with an external database |
| `backup.existingClaim` | `""` | PersistentVolumeClaim you create for the backup files; required when enabled, and not removed on uninstall |
| `auth.session.idleMinutes`, `auth.session.lifetimeMinutes` | empty | Session idle and absolute expiry in minutes. Empty leaves them to the operating profile chosen in the web UI (defaults 60 and 480); a value here overrides the profile and is reported on the Compliance page if it misses the profile's limit |
| `gitlab.url` | `""` | https URL of the GitLab instance; empty lets an administrator connect GitLab in the web UI |
| `gitlab.existingSecret`, `gitlab.tokenKey` | `""`, `token` | Secret and key holding a `read_api` token; required with `gitlab.url`. Mounted into the API and worker |
| `gitlab.timeoutSeconds` | `15` | Per-request timeout |
| `gitlab.syncIntervalMinutes` | `360` | Scheduled collection interval (15 to 10080), or `0` for on request only; data older than twice this is stale |
| `ingress.enabled`, `ingress.className`, `ingress.host`, `ingress.tls` | disabled | Ingress to the web Service |
| `imagePullSecrets` | `[]` | Registry credentials |

## Security defaults

Pods run as non-root numeric UIDs with the `RuntimeDefault` seccomp profile, a read-only root filesystem, no privilege escalation, all capabilities dropped and only `/tmp` writable. Service account tokens are not mounted, and the chart creates no RBAC because Baluarte does not call the Kubernetes API.

## Smoke test

`deploy/helm/smoke-test.sh` lints the chart with each `ci/*-values.yaml`, builds the images, installs into a throwaway kind cluster with the dev database, checks the migration Job, runs `helm test` and security checks, upgrades (re-running migrations and keeping the generated password), uninstalls and deletes the cluster. Requires Docker, kind, Helm and kubectl.
