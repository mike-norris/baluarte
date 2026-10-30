# Deployment specification

## Common contract

One set of versioned OCI images: `baluarte-web`, `baluarte-app` (API and worker commands). Configuration through environment variables and mounted secrets, or for the identity provider and GitLab connection, the web UI setup wizard ([ADR-0008](architecture/ADR-0008-settings-in-the-database.md)). The same images run in Docker Compose and Kubernetes.

## Docker Compose

Web, API, worker, and PostgreSQL for development. Document required secrets, TLS reverse proxy, volume backup, and safe upgrades. Do not ship production default credentials. Backup and restore: [OPERATIONS.md](OPERATIONS.md).

## Kubernetes

Maintained Helm chart with web/API/worker Deployments, Services, Ingress configuration, optional external PostgreSQL, Secrets references, ConfigMaps, resource requests/limits, non-root security contexts, probes, migration Job, and PodDisruptionBudget as appropriate. Namespace-scoped RBAC only where necessary. The chart's optional development database has no backups; production uses an external PostgreSQL with its own backups and point-in-time recovery ([OPERATIONS.md](OPERATIONS.md#kubernetes-helm)).

## Release validation

CI builds images, tests Docker Compose startup, and installs chart into a disposable Kubernetes cluster (e.g. kind) for smoke tests. Verify schema migrations, worker scheduling, health, and graceful shutdown. Support air-gapped image loading and configurable registries.
