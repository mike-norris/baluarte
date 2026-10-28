# Baluarte

**Defend the Standard. Prove the Trust.**

Baluarte is a self-hosted, open-source governance and audit-evidence platform for **self-managed GitLab**. It discovers groups and projects, evaluates their configuration against versioned security baselines, records verifiable evidence, and (in a later phase) performs authorized remediation.

## Scope

Initial product scope is **GitLab only**. Docker Compose and Kubernetes/Helm are supported deployment targets, not governed resources. No GitHub, Bitbucket, or Kubernetes configuration scanning is planned for v0.1.

## Technology

Python 3.12+ / FastAPI / SQLAlchemy / Alembic / PostgreSQL; React / TypeScript / Vite / Tailwind; OCI images; Docker Compose and Helm. OIDC authentication. Apache or cloud services are not required. See [architecture](docs/ARCHITECTURE.md) and [requirements](docs/PRODUCT_REQUIREMENTS.md).

## Status

Planning foundation; **no application implementation yet**. See the [roadmap](docs/ROADMAP.md).

## Run locally

Requires Docker with Compose v2. The stack is for development only; it binds to 127.0.0.1 and is not a production configuration.

```sh
mkdir -p secrets && (umask 022 && openssl rand -base64 32 > secrets/postgres_password)
docker compose up -d --build --wait
```

- Web UI: http://127.0.0.1:8080
- API health: http://127.0.0.1:8000/api/v1/health (readiness, including migrations: `/api/v1/ready`)

Sign-in uses OIDC and is off by default, so pages that need data answer 503. To enable it, set `BALUARTE_PUBLIC_URL=http://127.0.0.1:8080`, the issuer, client ID and group variables listed in [backend/README.md](backend/README.md#configuration) in a `.env` file, and register `http://127.0.0.1:8080/api/v1/auth/callback` with your provider.

The `migrate` service applies database migrations before the API starts. The password file must be readable by the containers' non-root users, so it is created world-readable; keep it in a single-user development checkout only.

Stop with `docker compose down` (add `-v` to delete the database volume). `deploy/compose/smoke-test.sh` builds, checks and removes a throwaway copy of the stack. For Kubernetes, see the [Helm chart](deploy/helm/baluarte/README.md). For backend and frontend development without containers, see [backend/README.md](backend/README.md) and [frontend/README.md](frontend/README.md).

## License

Licensed under the Mozilla Public License 2.0. See [LICENSE](LICENSE).

## Contributing

Open a GitHub issue first for significant changes. All changes require tests, documentation updates where relevant, and review.
