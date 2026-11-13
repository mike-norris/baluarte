/**
 * Issue plans: the time rules on closed issues (docs/TIME_TRACKING.md, ADR-0011). A plan adds
 * or removes the "missing time" label and sets estimates, and is applied only once approved.
 */

import { getJson } from "./client";
import { postChange } from "./controls";
import type { LabelPlanState } from "./labels";
import type { Person } from "./policies";

export type IssuePlanState = LabelPlanState;
export type IssueAction = "add_label" | "remove_label" | "set_estimate";
export type IssueStepStatus = "pending" | "skipped" | "stale" | "applied" | "failed" | "not_run";

export interface IssueRules {
  missing_time_label: string;
  estimate_round_minutes: number;
  issue_types: string[];
}

export interface IssuePreview {
  rules: IssueRules;
  closed_issues: number;
  last_read_at: string | null;
  add_label: number;
  remove_label: number;
  set_estimate: number;
  commits_unknown: number;
  writes_enabled: boolean;
  max_steps: number;
}

export interface IssueProject {
  gitlab_id: number;
  path: string;
}

export interface IssueRef {
  iid: number;
  type: string | null;
  title: string | null;
  web_url: string | null;
}

export interface IssuePlanStep {
  step: number;
  action: IssueAction;
  project: IssueProject;
  issue: IssueRef;
  label: string | null;
  prior: { has_label: boolean; estimate_seconds: number; spent_seconds: number };
  intended: { has_label?: boolean; estimate_seconds?: number };
  status: IssueStepStatus;
  request_status: number | null;
  verified: boolean | null;
  message: string | null;
  finished_at: string | null;
}

export interface IssueManualItem {
  reason: string;
  message: string;
  project: IssueProject;
  issues: number[];
}

export interface IssuePlanSummary {
  id: number;
  state: IssuePlanState;
  scope: { groups: string[] };
  totals: Record<string, number>;
  steps: number;
  manual: number;
  plan_digest: string;
  created_by: Person;
  created_at: string;
  expires_at: string;
  decided_by: Person | null;
  decided_at: string | null;
  finished_at: string | null;
}

export interface IssuePlan extends IssuePlanSummary {
  rules: IssueRules;
  step_items: IssuePlanStep[];
  manual_items: IssueManualItem[];
  digest_valid: boolean;
  submitted_at: string | null;
  decision_note: string | null;
  review_required: boolean;
  self_approved: boolean;
  apply_started_at: string | null;
  failed_step: number | null;
  failure: string | null;
  progress: Record<string, number>;
  can_apply: { allowed: boolean; reasons: string[] };
}

export interface IssuePlanPage {
  items: IssuePlanSummary[];
  next_after_id: number | null;
}

export const ISSUE_PLANS = "/time/issue-plans";
export const ISSUE_PLANS_PAGE = 50;

export function issuePlanHref(id: number): string {
  return `${ISSUE_PLANS}/${String(id)}`;
}

/** Sentences for the API's reasons for refusing a plan change. */
export const issueRefusalReasons: Record<string, string> = {
  own_change: "you created this plan, so someone else must decide it",
  issue_writes_disabled: "issue writes are turned off for this deployment",
  write_token_missing: "no write token is set",
  not_approved: "the plan is not approved",
  expired: "the plan has expired",
  plan_expired: "the plan has expired",
  digest_mismatch: "the plan changed since you reviewed it",
  nothing_to_plan: "every closed issue in scope already follows the rules",
  scope_too_large: "the plan would be too large; name fewer groups",
  nothing_to_apply: "the plan has nothing to apply",
};

export const issueApplyBlockText: Record<string, string> = {
  issue_writes_disabled:
    "Issue writes are turned off for this deployment (BALUARTE_ISSUE_WRITES is not true).",
  write_token_missing: "No write token is set. An administrator can add one in Settings.",
  not_approved: "The plan must be approved before it can be applied.",
  expired: "The plan has expired. Create a new plan.",
  nothing_to_apply: "The plan has only issues to fix by hand.",
};

export function issueApplyBlocks(reasons: readonly string[]): string[] {
  return reasons.map((code) => issueApplyBlockText[code] ?? `${code.replace(/_/g, " ")}.`);
}

export const issueActionLabels: Record<IssueAction, string> = {
  add_label: "Add label",
  remove_label: "Remove label",
  set_estimate: "Set estimate",
};

export const issueStepStatusLabels: Record<IssueStepStatus, string> = {
  pending: "Not run yet",
  skipped: "Already in place",
  stale: "Changed since the plan; left alone",
  applied: "Applied",
  failed: "Failed",
  not_run: "Not run",
};

/** Seconds as GitLab writes durations: "1h 30m", "30m", "0m". */
export function durationText(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${String(rest)}m`;
  return rest === 0 ? `${String(hours)}h` : `${String(hours)}h ${String(rest)}m`;
}

/** What a step changes, in words. */
export function stepChange(step: IssuePlanStep): string {
  if (step.action === "set_estimate") {
    return `Estimate ${durationText(step.intended.estimate_seconds ?? 0)} (spent ${durationText(step.prior.spent_seconds)})`;
  }
  return `${issueActionLabels[step.action]} ${step.label ?? ""}`.trim();
}

export function getIssuePreview(group: string | null, signal?: AbortSignal): Promise<IssuePreview> {
  return getJson<IssuePreview>("/api/v1/issue-plans/preview", { group }, signal);
}

export function listIssuePlans(
  state: IssuePlanState | undefined,
  afterId: number | null,
  signal?: AbortSignal,
): Promise<IssuePlanPage> {
  return getJson<IssuePlanPage>(
    "/api/v1/issue-plans",
    { state, after_id: afterId, limit: ISSUE_PLANS_PAGE },
    signal,
  );
}

export function getIssuePlan(id: number, signal?: AbortSignal): Promise<IssuePlan> {
  return getJson<IssuePlan>(`/api/v1/issue-plans/${String(id)}`, {}, signal);
}

export function createIssuePlan(groups: string[], csrfToken: string): Promise<IssuePlan> {
  return postChange(
    "/api/v1/issue-plans",
    groups.length > 0 ? { scope: { groups } } : {},
    csrfToken,
    issueRefusalReasons,
  );
}

export function issuePlanAction(
  id: number,
  action: "submit" | "apply",
  csrfToken: string,
): Promise<IssuePlan> {
  return postChange(
    `/api/v1/issue-plans/${String(id)}/${action}`,
    {},
    csrfToken,
    issueRefusalReasons,
  );
}

export function approveIssuePlan(
  id: number,
  planDigest: string,
  note: string,
  csrfToken: string,
): Promise<IssuePlan> {
  const text = note.trim();
  return postChange(
    `/api/v1/issue-plans/${String(id)}/approve`,
    text ? { plan_digest: planDigest, note: text } : { plan_digest: planDigest },
    csrfToken,
    issueRefusalReasons,
  );
}

export function rejectIssuePlan(id: number, note: string, csrfToken: string): Promise<IssuePlan> {
  return postChange(
    `/api/v1/issue-plans/${String(id)}/reject`,
    { note: note.trim() },
    csrfToken,
    issueRefusalReasons,
  );
}
