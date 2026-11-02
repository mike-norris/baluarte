/** The audit trail (``audit_log:read``: admins and auditors). See docs/AUDIT_EVIDENCE.md. */

import { getJson } from "./client";

export const AUDIT_PAGE = 50;

export interface AuditEvent {
  id: number;
  occurred_at: string;
  /** ``user:<id>``, or a ``system:`` actor for work Baluarte did itself. */
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  details: Record<string, unknown>;
  details_digest: string;
  /** The SHA-256 of ``details`` recomputed now still equals ``details_digest``. */
  digest_valid: boolean;
}

export interface AuditEventPage {
  items: AuditEvent[];
  next_before_id: number | null;
}

export interface AuditFilter {
  action?: string | undefined;
  actor?: string | undefined;
  /** Inclusive, ISO 8601 with a time zone. */
  since?: string | undefined;
  /** Exclusive, ISO 8601 with a time zone. */
  until?: string | undefined;
}

export function listAuditEvents(
  filter: AuditFilter,
  beforeId: number | null,
  signal?: AbortSignal,
): Promise<AuditEventPage> {
  return getJson(
    "/api/v1/audit-events",
    { ...filter, before_id: beforeId, limit: AUDIT_PAGE },
    signal,
  );
}

/** Every action recorded so far, for the filter. */
export function listAuditActions(signal?: AbortSignal): Promise<string[]> {
  return getJson("/api/v1/audit-events/actions", undefined, signal);
}
