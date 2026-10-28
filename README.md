# Baluarte

**Defend the Standard. Prove the Trust.**

Baluarte is a self-hosted, open-source governance and audit-evidence platform for **self-managed GitLab**. It discovers groups and projects, evaluates their configuration against versioned security baselines, records verifiable evidence, and (in a later phase) performs authorized remediation.

## Scope

Initial product scope is **GitLab only**. Docker Compose and Kubernetes/Helm are supported deployment targets, not governed resources. No GitHub, Bitbucket, or Kubernetes configuration scanning is planned for v0.1.

## Technology

Python 3.12+ / FastAPI / SQLAlchemy / Alembic / PostgreSQL; React / TypeScript / Vite / Tailwind; OCI images; Docker Compose and Helm. OIDC authentication. Apache or cloud services are not required. See [architecture](docs/ARCHITECTURE.md) and [requirements](docs/PRODUCT_REQUIREMENTS.md).

## Status

Planning foundation; **no application implementation yet**. See the [roadmap](docs/ROADMAP.md).

## License

Licensed under the Mozilla Public License 2.0. See [LICENSE](LICENSE).

## Contributing

Open a GitHub issue first for significant changes. All changes require tests, documentation updates where relevant, and review.
