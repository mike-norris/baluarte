# ADR-0001: Modular monolith with shared API/worker image

- Status: Accepted
- Date: 2026-10-28

## Context

Baluarte is self-hosted by small security and platform teams, often on-premises. Every extra service adds deployment, upgrade, and failure-mode burden for operators, and the expected load (one GitLab instance, periodic collection) does not justify independent scaling of many components.

## Decision

Build one Python backend organized into modules (`auth`, `integrations/gitlab`, `discovery`, `inventory`, `policies`, `evaluation`, `findings`, `evidence`, `reporting`, `jobs`, `admin`). The FastAPI API and the background worker ship in one versioned OCI image and differ only by start command. The React frontend ships as a separate static-serving image. Modules communicate through in-process interfaces, not network calls.

## Consequences

- Two images to build, sign, and scan; one backend dependency set.
- API and worker scale independently as separate Deployments/containers running the same image.
- Module boundaries are enforced by convention and review, not by the network; GitLab-specific models stay in `integrations/gitlab`.
- Microservices, Redis, Celery, Kafka, or Kubernetes runtime dependencies require a new ADR.

## Revisit when

Measured load or team structure shows a module needs independent release cadence or scaling that a separate worker command cannot provide.
