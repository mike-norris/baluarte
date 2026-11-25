/** Words and small helpers shared by the template views (docs/CONFIGURATION_TEMPLATES.md). */

import { ApiError } from "../api/client";
import type { TargetType } from "../api/policies";
import type { DriftStatus, Source, Support } from "../api/templates";
import { useAuth } from "../auth/context";
import { canWritePolicies } from "../policies/format";
import type { Tone } from "../ui/Badge";

export const settingLabels: Record<string, string> = {
  visibility: "Visibility",
  default_branch_protected: "Default branch protected",
  "merge_requests.min_approvals": "Approvals required",
  "merge_requests.forbid_author_approval": "Authors cannot approve",
  "merge_requests.forbid_committer_approval": "Committers cannot approve",
  "merge_requests.reset_approvals_on_push": "Approvals reset on push",
  "merge_requests.forbid_override": "Approval rules cannot be overridden per merge request",
  security_features: "Security features enabled",
  labels_consistent: "Labels follow the taxonomy",
};

export const supportLabels: Record<Support, string> = {
  supported: "Supported",
  unsupported: "No equivalent",
  plan: "Depends on the plan",
};

export const originLabels: Record<Source["origin"], string> = {
  override: "override",
  template: "template",
  extends: "inherited through extends",
};

export function valueText(value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value as string | number);
}

export function placeText(target: { target_type: TargetType; path: string }): string {
  if (target.target_type === "instance") return "Whole connection";
  return `${target.target_type === "group" ? "Group" : "Project"} ${target.path}`;
}

export function useCsrf(): {
  token: string | null;
  canWrite: boolean;
  canRequest: boolean;
  canApprove: boolean;
  canPlan: boolean;
} {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const permissions = user?.permissions ?? [];
  return {
    token: user?.csrf_token ?? null,
    canWrite: canWritePolicies(user),
    canRequest: permissions.includes("exception:request"),
    canApprove: permissions.includes("exception:approve"),
    canPlan: permissions.includes("changes:plan"),
  };
}

export function failureText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

export const driftLabels: Record<DriftStatus, string> = {
  aligned: "Aligned",
  drift: "Drift",
  excepted: "Excepted",
  unknown: "Unknown",
  unsupported: "Unsupported",
};

export const driftTones: Record<DriftStatus, Tone> = {
  aligned: "success",
  drift: "danger",
  excepted: "warning",
  unknown: "secondary",
  unsupported: "info",
};

export function sourceText(source: Source): string {
  return `${source.template} v${String(source.version)} (${originLabels[source.origin]}) on ${placeText(
    source.assigned_to,
  ).toLowerCase()}`;
}
