# ADR-0002: PostgreSQL as the background job queue

- Status: Accepted
- Date: 2026-10-28

## Context

Inventory collection, evaluation, and export run in the background and must survive restarts, report failures, and avoid duplicate work. PostgreSQL is already required for application data. Adding a broker (Redis, RabbitMQ, Kafka) would add another stateful service to deploy, secure, and back up.

## Decision

Store jobs as durable rows in PostgreSQL. Workers claim jobs with a time-bounded lease (for example `SELECT ... FOR UPDATE SKIP LOCKED` and a `lease_until` timestamp), record attempts, and retry with bounded backoff. Each job carries an idempotency key. Failed and expired jobs remain visible for operators.

## Consequences

- Delivery is at-least-once, not exactly-once. Every handler must be idempotent.
- Expired leases are reclaimed, so a slow worker may overlap with a retry; handlers must tolerate this.
- Throughput is bounded by database capacity, which is adequate for single-instance collection workloads.
- Job state is covered by the same backup and migration process as application data.

## Revisit when

Measured job volume or latency exceeds what PostgreSQL leasing handles without affecting API queries.
