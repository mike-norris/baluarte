/** Types and calls for dry-run remediation plans (docs/REMEDIATION.md, BAL-031). */

import { ApiError, getJson } from "./client";
import { changeFailure, detailText } from "./send";

/** ``expired`` once ``expires_at`` has passed, whether or not the row says so yet. */
export type PlanState = "draft" | "submitted" | "approved" | "rejected" | "expired";

export const planStates: readonly PlanState[] = [
  "draft",
  "submitted",
  "approved",
  "rejected",
  "expired",
];

// Limits the API enforces (backend/src/baluarte/db/models.py, remediation/plans.py).
export const MAX_PLAN_STEPS = 100;
export const MAX_PLAN_HOURS = 72;
export const DEFAULT_PLAN_HOURS = 24;
export const MAX_NOTE = 2000;
export const PLANS_PAGE = 50;

export const CAN_PLAN = "remediation:plan";
export const CAN_APPROVE = "remediation:approve";

export interface PlanRequest {
  method: string;
  path: string;
  body?: Record<string, unknown>;
  expect?: Record<string, unknown>;
}

export interface PlanStep {
  step: number;
  action: string;
  target: { project_gitlab_id: number; project_path: string };
  finding_id: number;
  evaluation_id: number;
  observation_id: number;
  observation_source: string;
  observation_digest: string | null;
  observed_at: string;
  policy: { name: string; version: number; policy_id: number };
  rule: { id: string; type: string; severity: string };
  current: Record<string, unknown>;
  intended: Record<string, unknown>;
  expected: unknown;
  request: PlanRequest;
  verify: PlanRequest;
  preconditions: string[];
  effects: string[];
  requires: { role: string; tier: string };
  idempotency_key: string;
}

export interface SkippedFinding {
  finding_id: number;
  project_path?: string | null;
  policy_name?: string | null;
  rule_id?: string | null;
  reason_code: string;
  reason: string;
}

/** The stored plan document; its canonical JSON is what ``plan_digest`` covers. */
export interface PlanDocument {
  format: string;
  dry_run: boolean;
  instance: string;
  created_at: string;
  expires_at: string;
  created_by: string;
  scope: Record<string, unknown>;
  steps: PlanStep[];
  skipped: SkippedFinding[];
  excluded_actions: Record<string, string>;
  execution: string;
}

export interface PlanSummary {
  id: number;
  state: PlanState;
  steps: number;
  skipped: number;
  plan_digest: string;
  created_by_user_id: number;
  created_at: string;
  expires_at: string;
  submitted_at: string | null;
  decided_by_user_id: number | null;
  decided_at: string | null;
  decision_note: string | null;
  approved_digest: string | null;
}

export interface Plan extends PlanSummary {
  /** Recomputed from the stored plan on every read. */
  digest_valid: boolean;
  /** Why approval would be refused now; empty when the plan still matches its evidence. */
  stale_reasons: string[];
  summary: string[];
  plan: PlanDocument;
}

export interface PlanPage {
  items: PlanSummary[];
  next_after_id: number | null;
}

/** Explicit finding IDs, or filters; the API takes one kind, never both. */
export interface PlanScope {
  finding_ids?: number[];
  policy?: string;
  rule?: string;
  project_gitlab_id?: number;
}

/** A refused change, with the plan's own reasons when the API gives them. */
export class PlanError extends ApiError {
  readonly reasons: string[];
  readonly skipped: SkippedFinding[];

  constructor(status: number, message: string, reasons: string[], skipped: SkippedFinding[]) {
    super(status, message);
    this.name = "PlanError";
    this.reasons = reasons;
    this.skipped = skipped;
  }
}

interface StructuredDetail {
  message?: unknown;
  reasons?: unknown;
  skipped?: unknown;
}

async function failure(response: Response): Promise<PlanError> {
  let detail: unknown = null;
  try {
    detail = ((await response.json()) as { detail?: unknown }).detail;
  } catch {
    detail = null;
  }
  // Staleness (409) and "nothing to plan" (422) come as {message, reasons | skipped}.
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const structured = detail as StructuredDetail;
    const message = typeof structured.message === "string" ? structured.message : null;
    const reasons = Array.isArray(structured.reasons)
      ? structured.reasons.filter((reason): reason is string => typeof reason === "string")
      : [];
    const skipped = Array.isArray(structured.skipped)
      ? (structured.skipped as SkippedFinding[])
      : [];
    return new PlanError(
      response.status,
      changeFailure(response.status, message),
      reasons,
      skipped,
    );
  }
  return new PlanError(response.status, changeFailure(response.status, detailText(detail)), [], []);
}

async function post(path: string, body: unknown, csrfToken: string): Promise<Plan> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) throw await failure(response);
  return (await response.json()) as Plan;
}

export function listPlans(
  state: PlanState | undefined,
  afterId: number | null,
  signal?: AbortSignal,
): Promise<PlanPage> {
  return getJson<PlanPage>(
    "/api/v1/remediation/plans",
    { state, after_id: afterId, limit: PLANS_PAGE },
    signal,
  );
}

export function getPlan(id: number, signal: AbortSignal): Promise<Plan> {
  return getJson<Plan>(`/api/v1/remediation/plans/${String(id)}`, {}, signal);
}

export function createPlan(scope: PlanScope, hours: number, csrfToken: string): Promise<Plan> {
  return post("/api/v1/remediation/plans", { scope, expires_in_hours: hours }, csrfToken);
}

export function submitPlan(id: number, csrfToken: string): Promise<Plan> {
  return post(`/api/v1/remediation/plans/${String(id)}/submit`, {}, csrfToken);
}

/** Approval names the digest the approver reviewed; the API refuses any other. */
export function approvePlan(
  id: number,
  planDigest: string,
  note: string,
  csrfToken: string,
): Promise<Plan> {
  const text = note.trim();
  return post(
    `/api/v1/remediation/plans/${String(id)}/approve`,
    text ? { plan_digest: planDigest, note: text } : { plan_digest: planDigest },
    csrfToken,
  );
}

export function rejectPlan(id: number, note: string, csrfToken: string): Promise<Plan> {
  return post(`/api/v1/remediation/plans/${String(id)}/reject`, { note: note.trim() }, csrfToken);
}
