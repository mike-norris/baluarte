import type { FindingFilters } from "../api/evaluations";
import {
  MAX_PLAN_STEPS,
  planStates,
  type PlanScope,
  type PlanState,
  type PlanSummary,
} from "../api/remediation";
import type { CurrentUser } from "../auth/session";

export const DRY_RUN_NOTICE =
  "Dry run: Baluarte makes no change to GitLab. A plan describes the changes that would fix " +
  "failing findings; executing a plan is not available in this version, so approving one " +
  "changes nothing in GitLab.";

export const planStateLabels: Record<PlanState, string> = {
  draft: "Draft",
  submitted: "Submitted",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired",
};

export const planStateMeaning: Record<PlanState, string> = {
  draft: "Only its creator can see it change; the creator submits it for approval.",
  submitted: "Waiting for someone other than its creator to approve or reject it.",
  approved:
    "Approved for the digest shown. Nothing has been changed in GitLab: execution is not " +
    "available in this version. Apply the steps by hand in GitLab, then run project discovery " +
    "to verify them.",
  rejected: "Rejected; nothing will be done with it.",
  expired: "Past its expiry; it can no longer be submitted or approved.",
};

export const actionLabels: Record<string, string> = {
  restrict_project_visibility: "Restrict project visibility",
  protect_default_branch: "Protect the default branch",
};

/** Short labels for the API's skip reason codes; the API's own sentence is shown beside them. */
const skipLabels: Record<string, string> = {
  not_found: "Finding not found",
  resolved: "Finding resolved",
  excepted: "Finding excepted",
  latest_unknown: "Latest result unknown",
  latest_unsupported: "Latest result unsupported",
  latest_excepted: "Latest result excepted",
  latest_pass: "Latest result passes",
  project_missing: "Project missing",
  project_inaccessible: "Project inaccessible",
  project_archived: "Project archived",
  evidence_missing: "No readable evidence",
  evidence_digest_mismatch: "Evidence altered",
  evidence_not_latest: "Newer evidence exists",
  rule_missing: "Rule not in the policy version",
  value_unknown: "Observed value unclear",
  would_widen_access: "Would widen access",
  not_required: "Not required by the rule",
  branch_changed: "Default branch changed",
  no_action: "No remediation action",
};

export function skipLabel(code: string): string {
  return skipLabels[code] ?? code;
}

export function isPlanState(value: string | null): value is PlanState {
  return planStates.some((state) => state === value);
}

export function hasPermission(user: CurrentUser, permission: string): boolean {
  return user.permissions.includes(permission);
}

/** Whether the signed-in user created the plan; the API refuses the creator's decision anyway. */
export function isCreator(plan: PlanSummary, user: CurrentUser): boolean {
  return user.user_id === plan.created_by_user_id;
}

export function userLabel(id: number | null, user: CurrentUser | null): string {
  if (id === null) return "Nobody yet";
  const you = user?.user_id === id ? " (you)" : "";
  return `User ${String(id)}${you}`;
}

const MAX_NAME = 63;

function name(value: string | null): string | undefined {
  const text = value?.trim();
  return text && text.length <= MAX_NAME ? text : undefined;
}

/** Finding IDs from "5, 7 9": positive whole numbers, deduplicated; null when any is invalid. */
export function parseFindingIds(text: string): number[] | null {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const ids: number[] = [];
  for (const part of parts) {
    const id = Number(part);
    if (!/^\d+$/.test(part) || !Number.isSafeInteger(id) || id <= 0) return null;
    if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** The plan scope a URL asks for: ``finding`` IDs, or ``project``, ``policy`` and ``rule``. */
export function readPlanScope(params: URLSearchParams): PlanScope {
  const findings = parseFindingIds(params.getAll("finding").join(","));
  if (findings && findings.length > 0) return { finding_ids: findings.slice(0, MAX_PLAN_STEPS) };
  const scope: PlanScope = {};
  const project = Number(params.get("project"));
  if (Number.isSafeInteger(project) && project > 0) scope.project_gitlab_id = project;
  const policy = name(params.get("policy"));
  if (policy) scope.policy = policy;
  const rule = name(params.get("rule"));
  if (rule) scope.rule = rule;
  return scope;
}

export function writePlanScope(scope: PlanScope): URLSearchParams {
  const params = new URLSearchParams();
  for (const id of scope.finding_ids ?? []) params.append("finding", String(id));
  if (scope.project_gitlab_id !== undefined) {
    params.set("project", String(scope.project_gitlab_id));
  }
  if (scope.policy) params.set("policy", scope.policy);
  if (scope.rule) params.set("rule", scope.rule);
  return params;
}

export interface PlanLink {
  href: string;
  /** Findings filters a plan scope cannot express, so the plan ignores them. */
  dropped: string[];
}

/**
 * A link to the plan form for a findings view, or null when the view has no filter a plan can
 * use: a plan always names a project, policy, rule or findings, never "everything".
 */
export function findingsPlanLink(filters: FindingFilters): PlanLink | null {
  const scope: PlanScope = {};
  if (filters.project !== undefined) scope.project_gitlab_id = filters.project;
  if (filters.policy) scope.policy = filters.policy;
  if (filters.rule) scope.rule = filters.rule;
  if (Object.keys(scope).length === 0) return null;
  const dropped: string[] = [];
  if (filters.state) dropped.push("state");
  if (filters.outcome) dropped.push("outcome");
  if (filters.severity) dropped.push("severity");
  return { href: `/remediation/new?${writePlanScope(scope).toString()}`, dropped };
}

/** Values of a plan step's ``current``, ``intended`` or ``expected`` as "key: value" lines. */
export function describeValues(value: unknown): string[] {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return Object.entries(value as Record<string, unknown>).map(
      ([key, item]) => `${key}: ${typeof item === "string" ? item : JSON.stringify(item)}`,
    );
  }
  return [typeof value === "string" ? value : JSON.stringify(value)];
}

/** The request as it would be sent: method, path and pretty-printed JSON body. */
export function requestText(request: { method: string; path: string; body?: unknown }): string {
  const head = `${request.method} ${request.path}`;
  return request.body === undefined ? head : `${head}\n${JSON.stringify(request.body, null, 2)}`;
}
