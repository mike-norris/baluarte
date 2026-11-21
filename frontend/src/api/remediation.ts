/** Types and calls for remediation plans and applying them (docs/REMEDIATION.md). */

import { ApiError, getJson } from "./client";
import type { WriteTokenStatus } from "./labels";
import { changeFailure, detailText, sendJson } from "./send";
import { scopeHeaders } from "./workspaceChoice";

/** ``expired`` once ``expires_at`` has passed, whether or not the row says so yet. */
export type PlanState =
  "draft" | "submitted" | "approved" | "rejected" | "expired" | "applying" | "applied" | "failed";

export const planStates: readonly PlanState[] = [
  "draft",
  "submitted",
  "approved",
  "rejected",
  "expired",
  "applying",
  "applied",
  "failed",
];

/** A step's outcome while and after the plan is applied. */
export type StepStatus = "pending" | "skipped" | "out_of_date" | "applied" | "failed" | "not_run";

export interface StepProgress {
  step: number;
  status: StepStatus;
  /** The write's HTTP status; null when nothing was written. */
  request_status?: number | null;
  /** Whether a read with the read-only token showed the intended value. */
  verified?: boolean | null;
  observed?: Record<string, unknown> | null;
  message?: string | null;
  finished_at?: string | null;
}

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
  applied_by_user_id: number | null;
  apply_started_at: string | null;
  finished_at: string | null;
  failed_step: number | null;
  failure: string | null;
}

export interface Plan extends PlanSummary {
  /** Recomputed from the stored plan on every read. */
  digest_valid: boolean;
  /** Why approval or applying would be refused now; empty when the plan matches its evidence. */
  stale_reasons: string[];
  summary: string[];
  plan: PlanDocument;
  can_apply: { allowed: boolean; reasons: string[] };
  progress: StepProgress[];
}

/** Why a plan can't be applied now, as sentences. */
export const applyBlockText: Record<string, string> = {
  remediation_writes_disabled:
    "Remediation writes are turned off for this deployment (BALUARTE_REMEDIATION_WRITES is not true).",
  write_token_missing:
    "No remediation write token is set. An administrator can add one in Settings.",
  expired: "The plan has expired. Create a new plan from current findings.",
  not_approved: "The plan must be approved before it can be applied.",
  stale: "The plan no longer matches its findings and evidence. Create a new plan.",
  projects_not_current:
    "The projects collection is not current. Run project discovery, then try again.",
  already_applying: "Another plan is applying the same change. Wait for it to finish.",
};

/** A sentence for each reason the plan can't be applied; unknown codes read as words. */
export function applyBlocks(reasons: readonly string[]): string[] {
  return reasons.map((code) => applyBlockText[code] ?? `${code.replace(/_/g, " ")}.`);
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
        ...scopeHeaders(),
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

/** Queues the applier; the plan comes back ``applying``. */
export function applyPlan(id: number, csrfToken: string): Promise<Plan> {
  return post(`/api/v1/remediation/plans/${String(id)}/apply`, {}, csrfToken);
}

const TOKEN_PATH = "/api/v1/settings/remediation-write-token";

/** ``writes_enabled`` is ``BALUARTE_REMEDIATION_WRITES`` for this deployment. */
export function getRemediationWriteToken(signal?: AbortSignal): Promise<WriteTokenStatus> {
  return getJson<WriteTokenStatus>(TOKEN_PATH, {}, signal);
}

export async function putRemediationWriteToken(
  token: string,
  csrfToken: string,
): Promise<WriteTokenStatus> {
  const status = await sendJson<WriteTokenStatus>("PUT", TOKEN_PATH, { token }, csrfToken);
  if (status === null) throw new Error("The API returned no content.");
  return status;
}

export async function removeRemediationWriteToken(csrfToken: string): Promise<void> {
  await sendJson("DELETE", TOKEN_PATH, undefined, csrfToken);
}
