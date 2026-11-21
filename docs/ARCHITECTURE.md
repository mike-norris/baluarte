# Architecture

## Deployment model

Modular monolith with React web client, FastAPI service, Python worker, and PostgreSQL ([ADR-0001](architecture/ADR-0001-modular-monolith.md)), serving several isolated workspaces, each with GitLab and GitHub connections ([ADR-0013](architecture/ADR-0013-workspaces-and-connections.md)). API and worker share one versioned OCI backend image with different commands. Frontend has its own static-serving image. Docker Compose and Helm deploy the same images.

```text
Browser -> TLS ingress/proxy -> React UI -> FastAPI -> PostgreSQL
                                         |             ^
                                         v             |
                                     Job records -> Worker -> provider adapters -> GitLab and GitHub APIs
                                         ^
                       provider webhooks (validated, idempotent; roadmap M13)
```

## Modules

Today: `auth`, `integrations/gitlab`, `policy` (baselines, evaluation, exceptions), `controls`, `changes`, `evidence`, `labels`, `remediation`, `scope`, `manager`, `timetracking`, `compliance`, `notifications`, `jobs`, `api`.

Planned ([ROADMAP.md](ROADMAP.md)): `workspaces` and `connections`, `integrations/github`, `templates` and `bulk` (configuration templates, bulk changes, drift), `register` (auditable issues), `assessments`, `planning` (portfolios and initiatives), `time` (native entries and approvals, extending `timetracking`), `finance` (classification, rates, costing, period closing) and `reporting`.

Provider-specific API models stay in the provider adapters; everything else works on normalized records that keep the provider's IDs, URLs, timestamps and original payload ([ADR-0014](architecture/ADR-0014-provider-integrations.md)). Personal and financial data have their own permissions.

## Contracts

- API: `/api/v1`; typed request/response schemas; OpenAPI generated from FastAPI.
- Background jobs: durable PostgreSQL records, leased claims, retries, idempotency keys, and visibility into failures. No exactly-once guarantee; design handlers to be idempotent.
- Inventory: upsert by provider resource ID plus connection; mark missing/inaccessible only after controlled reconciliation. Every row belongs to one workspace.
- Money: decimals with a currency per amount, effective-dated rates, documented rounding; closed periods change only through audited adjustments.
- Evaluation: immutable rule version + target + observation + outcome; explicit unknown and unsupported.
- Controls: a built-in catalogue maps rule types to SOC 2, ISO 27001 and NIST 800-53 controls; organization changes are append-only and reviewed like baseline changes, and control status is derived from the latest run, never stored ([CONTROL_MAPPING.md](CONTROL_MAPPING.md)).
- Evidence: source endpoint/reference, collected_at UTC, collection job, normalized payload, SHA-256 digest, and export manifest. Hashing is tamper-evidence, not immutable storage by itself.
- Authentication: OIDC, server-enforced RBAC; never trust client roles.

## Deployment

Docker Compose for local/small on-prem installs; Helm for Kubernetes. Prefer external PostgreSQL in production; chart must support external DB and optional dev DB. Non-root, read-only filesystems where feasible, resource limits, health probes, secret references, migration job, graceful shutdown. Do not require cluster-admin or host mounts.

## Architecture decision records

Create `docs/architecture/ADR-XXXX-*.md` when changing foundational choices. Accepted decisions are indexed in [docs/architecture](architecture/README.md): modular monolith, PostgreSQL job queue, read-only v0.1, workspaces and several connections (superseding single GitLab instance), a common provider layer, no cloud dependency, server-side OIDC sessions, and the constraints on every write path.
