# Architecture

## Deployment model

Modular monolith with React web client, FastAPI service, Python worker, and PostgreSQL. API and worker share one versioned OCI backend image with different commands. Frontend has its own static-serving image. Docker Compose and Helm deploy the same images.

```text
Browser -> TLS ingress/proxy -> React UI -> FastAPI -> PostgreSQL
                                         |             ^
                                         v             |
                                     Job records -> Worker -> GitLab REST API
```

## Modules

`auth`, `integrations/gitlab`, `discovery`, `inventory`, `policies`, `evaluation`, `findings`, `evidence`, `reporting`, `jobs`, `admin`. Keep GitLab-specific API models in the integration layer; do not implement other providers in v0.1.

## Contracts

- API: `/api/v1`; typed request/response schemas; OpenAPI generated from FastAPI.
- Background jobs: durable PostgreSQL records, leased claims, retries, idempotency keys, and visibility into failures. No exactly-once guarantee; design handlers to be idempotent.
- Inventory: upsert by GitLab numeric resource ID plus instance ID; mark missing/inaccessible only after controlled reconciliation.
- Evaluation: immutable rule version + target + observation + outcome; explicit unknown and unsupported.
- Evidence: source endpoint/reference, collected_at UTC, collection job, normalized payload, SHA-256 digest, and export manifest. Hashing is tamper-evidence, not immutable storage by itself.
- Authentication: OIDC, server-enforced RBAC; never trust client roles.

## Deployment

Docker Compose for local/small on-prem installs; Helm for Kubernetes. Prefer external PostgreSQL in production; chart must support external DB and optional dev DB. Non-root, read-only filesystems where feasible, resource limits, health probes, secret references, migration job, graceful shutdown. Do not require cluster-admin or host mounts.

## Architecture decision records

Create `docs/architecture/ADR-XXXX-*.md` when changing foundational choices. Accepted decisions are indexed in [docs/architecture](architecture/README.md): modular monolith, PostgreSQL job queue, read-only v0.1, single GitLab instance, no cloud dependency, server-side OIDC sessions.
