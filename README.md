# Baluarte

**Defend the Standard. Prove the Trust.**

Baluarte is a self-hosted, open-source governance and audit-evidence platform for **self-managed GitLab**. It discovers groups and projects, evaluates their configuration against versioned security baselines, records verifiable evidence, and (in a later phase) performs authorized remediation.

## Scope

Initial product scope is **GitLab only**. Docker Compose and Kubernetes/Helm are supported deployment targets, not governed resources. No GitHub, Bitbucket, or Kubernetes configuration scanning is planned for v0.1.

## Technology

Python 3.12+ / FastAPI / SQLAlchemy / Alembic / PostgreSQL; React / TypeScript / Vite / Tailwind; OCI images; Docker Compose and Helm. OIDC authentication. Apache or cloud services are not required. See [architecture](docs/ARCHITECTURE.md) and [requirements](docs/PRODUCT_REQUIREMENTS.md).

## Status

Pre-release (v0.1 in development): read-only GitLab inventory, policy evaluation, findings, exceptions, evidence export and dry-run remediation plans. See the [roadmap](docs/ROADMAP.md).

## Run locally

Requires Docker with Compose v2. The stack is for development only; it binds to 127.0.0.1 and is not a production configuration.

```sh
mkdir -p secrets && (umask 022 && openssl rand -base64 32 > secrets/postgres_password \
  && openssl rand -base64 32 > secrets/secret_key)
docker compose up -d --build --wait
docker compose logs api | grep 'setup code'
```

- Web UI: http://127.0.0.1:8080
- API health: http://127.0.0.1:8000/api/v1/health (readiness, including migrations: `/api/v1/ready`)

Open the web UI and follow the setup wizard. It asks for the one-time setup code from the API log (or run `docker compose exec api baluarte-setup-code` for a new one), then for your identity provider and, optionally, your GitLab URL and `read_api` token. Register `http://127.0.0.1:8080/api/v1/auth/callback` with the provider. Setup completes when the first user in an admin group signs in; after that, admins change these settings on the Settings page.

`secrets/secret_key` seals the client secret and GitLab token in the database. Keep it with your database backups: without it they must be entered again. See [docs/SECURITY.md](docs/SECURITY.md#setup-and-secrets-entered-in-the-web-ui).

To configure sign-in in the deployment instead, set `BALUARTE_PUBLIC_URL=http://127.0.0.1:8080` and the issuer, client ID and group variables listed in [backend/README.md](backend/README.md#configuration) in a `.env` file. Settings from the deployment take precedence and are read-only in the UI.

The `migrate` service applies database migrations before the API and `worker` start. The secret files must be readable by the containers' non-root users, so they are created world-readable; keep them in a single-user development checkout only.

To set the GitLab connection in the deployment rather than the UI, follow the steps at the top of `deploy/compose/gitlab.yaml`. Stop with `docker compose down` (add `-v` to delete the database volume). `deploy/compose/smoke-test.sh` builds, checks and removes a throwaway copy of the stack. For Kubernetes, see the [Helm chart](deploy/helm/baluarte/README.md). Backups, restore and retention: [docs/OPERATIONS.md](docs/OPERATIONS.md). For backend and frontend development without containers, see [backend/README.md](backend/README.md) and [frontend/README.md](frontend/README.md).

## License

Licensed under the Mozilla Public License 2.0. See [LICENSE](LICENSE).

## Contributing

Open a GitHub issue first for significant changes. All changes require tests, documentation updates where relevant, and review.
