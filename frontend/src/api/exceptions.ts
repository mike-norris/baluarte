/** Types and calls for policy exceptions (docs/POLICY_ENGINE.md#exceptions, BAL-033). */

import { getJson } from "./client";
import { postJson } from "./send";

/** ``expired`` once ``expires_at`` has passed, whether or not the row says so yet. */
export type ExceptionState = "requested" | "approved" | "rejected" | "revoked" | "expired";

export const exceptionStates: readonly ExceptionState[] = [
  "requested",
  "approved",
  "rejected",
  "revoked",
  "expired",
];

// Limits the API enforces (backend/src/baluarte/policy/exceptions.py, db/models.py).
export const MIN_JUSTIFICATION = 10;
export const MAX_JUSTIFICATION = 4000;
export const MAX_NOTE = 2000;
export const MAX_EXCEPTION_DAYS = 365;
export const EXCEPTIONS_PAGE = 50;

export interface UserRef {
  user_id: number;
  display_name: string | null;
}

export interface PolicyException {
  id: number;
  project: { gitlab_id: number; path: string; state: string };
  policy_name: string;
  rule_id: string;
  justification: string;
  state: ExceptionState;
  /** Approved and not yet expired: failures it covers are stored as ``excepted``. */
  in_force: boolean;
  requested_by: UserRef;
  requested_at: string;
  expires_at: string;
  decided_by: UserRef | null;
  decided_at: string | null;
  decision_note: string | null;
  ended_by: UserRef | null;
  ended_at: string | null;
  end_note: string | null;
}

export interface ExceptionPage {
  items: PolicyException[];
  next_after_id: number | null;
}

export interface ExceptionFilters {
  state?: ExceptionState;
  project?: number;
  policy?: string;
  rule?: string;
}

export interface ExceptionRequest {
  project_gitlab_id: number;
  policy_name: string;
  rule_id: string;
  justification: string;
  /** ISO 8601 with a time zone, at most ``MAX_EXCEPTION_DAYS`` ahead. */
  expires_at: string;
}

export function listExceptions(
  filters: ExceptionFilters,
  afterId: number | null,
  signal?: AbortSignal,
): Promise<ExceptionPage> {
  return getJson<ExceptionPage>(
    "/api/v1/exceptions",
    { ...filters, after_id: afterId, limit: EXCEPTIONS_PAGE },
    signal,
  );
}

export function getException(id: number, signal: AbortSignal): Promise<PolicyException> {
  return getJson<PolicyException>(`/api/v1/exceptions/${String(id)}`, {}, signal);
}

export function requestException(
  request: ExceptionRequest,
  csrfToken: string,
): Promise<PolicyException> {
  return postJson<PolicyException>("/api/v1/exceptions", request, csrfToken);
}

export type ExceptionAction = "approve" | "reject" | "revoke";

/** Approve (note optional), reject or revoke (note required). */
export function changeException(
  id: number,
  action: ExceptionAction,
  note: string,
  csrfToken: string,
): Promise<PolicyException> {
  const text = note.trim();
  return postJson<PolicyException>(
    `/api/v1/exceptions/${String(id)}/${action}`,
    action === "approve" && !text ? {} : { note: text },
    csrfToken,
  );
}
