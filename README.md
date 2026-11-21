# Baluarte

**Defend the Standard. Prove the Trust.**

Baluarte is a self-hosted, open-source platform that extends **GitLab and GitHub** with security governance and bulk configuration management, compliance assessments and auditable evidence, engineering portfolio planning, developer time tracking, and CapEx/OpEx labor cost reporting. GitLab and GitHub stay the systems of record for code, issues, merge or pull requests and CI/CD; Baluarte connects those records to engineering, security, audit and finance work.

## Scope

Today Baluarte governs self-managed GitLab: it discovers groups and projects, evaluates their configuration against versioned baselines, records verifiable evidence, and applies approved fixes. GitHub, several connections per workspace, configuration templates and bulk changes, the auditable issue register, time approval, finance and portfolio planning follow in the order set out in the [roadmap](docs/ROADMAP.md). Docker Compose and Kubernetes/Helm are deployment targets, not governed resources; Bitbucket and Kubernetes configuration scanning are not planned.

## Technology

Python 3.12+ / FastAPI / SQLAlchemy / Alembic / PostgreSQL; React / TypeScript / Vite / Tabler (Bootstrap 5); OCI images; Docker Compose and Helm. OIDC authentication. Apache or cloud services are not required. See [architecture](docs/ARCHITECTURE.md) and [requirements](docs/PRODUCT_REQUIREMENTS.md).

## Status

v0.1.0: read-only GitLab inventory, policy evaluation and findings, reviewed governance changes, control mapping, evidence export, audit packages and an auditor report, notifications, and dry-run remediation plans. Since then, on main: approved remediation plans applied with a separate write token, and in-app help. See the [changelog](CHANGELOG.md) and the [roadmap](docs/ROADMAP.md). Released images and the Helm chart are published to `ghcr.io/mike-norris`; see [releases](docs/DEPLOYMENT.md#releases).

## Try it

To evaluate the released version, follow the [quickstart](docs/QUICKSTART.md): the released images, your identity provider and a read-only GitLab token give you findings and an evidence export in about fifteen minutes. For a server, use the [Helm chart](deploy/helm/baluarte/README.md) and the [hardening checklist](docs/HARDENING.md).

## Run locally

To build from source instead: requires Docker with Compose v2. The stack is for development only; it binds to 127.0.0.1 and is not a production configuration.

```sh
mkdir -p secrets && (umask 022 && for name in postgres_password app_database_password backup_database_password secret_key; do
  [ -f secrets/$name ] || openssl rand -base64 32 > secrets/$name; done)
docker compose up -d --build --wait
docker compose logs api | grep 'setup code'
```

- Web UI: http://127.0.0.1:8080
- API health: http://127.0.0.1:8000/api/v1/health (readiness, including migrations: `/api/v1/ready`)

Open the web UI and follow the setup wizard. It asks for the one-time setup code from the API log (or run `docker compose exec api baluarte-setup-code` for a new one), then for your identity provider and, optionally, your GitLab URL and `read_api` token. Register `http://127.0.0.1:8080/api/v1/auth/callback` with the provider. Setup completes when the first user in an admin group signs in; after that, admins change these settings on the Settings page.

`secrets/postgres_password` is the schema owner's, used only by `migrate`. The API and worker connect as `baluarte_app` with `secrets/app_database_password`; it can read and write data but cannot change the schema or alter audit events.

`secrets/secret_key` seals the client secret and GitLab token in the database. Keep it with your database backups: without it they must be entered again. See [docs/SECURITY.md](docs/SECURITY.md#setup-and-secrets-entered-in-the-web-ui).

To configure sign-in in the deployment instead, set `BALUARTE_PUBLIC_URL=http://127.0.0.1:8080` and the issuer, client ID and group variables listed in [backend/README.md](backend/README.md#configuration) in a `.env` file. Settings from the deployment take precedence and are read-only in the UI.

The `migrate` service applies database migrations before the API and `worker` start. The secret files must be readable by the containers' non-root users, so they are created world-readable; keep them in a single-user development checkout only.

To set the GitLab connection in the deployment rather than the UI, follow the steps at the top of `deploy/compose/gitlab.yaml`. Stop with `docker compose down` (add `-v` to delete the database volume). `deploy/compose/smoke-test.sh` builds, checks and removes a throwaway copy of the stack. For Kubernetes, see the [Helm chart](deploy/helm/baluarte/README.md). Backups, restore and retention: [docs/OPERATIONS.md](docs/OPERATIONS.md). For backend and frontend development without containers, see [backend/README.md](backend/README.md) and [frontend/README.md](frontend/README.md).

## License

Licensed under the Mozilla Public License 2.0. See [LICENSE](LICENSE).

## Contributing

Open a GitHub issue first for significant changes. All changes require tests, documentation updates where relevant, and review.
