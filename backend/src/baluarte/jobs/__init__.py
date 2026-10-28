"""Durable background jobs leased from PostgreSQL (ADR-0002).

Delivery is at least once: a handler may run again after a crash or an expired lease, so every
handler must be idempotent.
"""
