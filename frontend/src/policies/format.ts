/**
 * Words, links and the recommended baseline for the Governance area (docs/POLICY_ENGINE.md).
 *
 * The UI says "baseline" for what the API calls a policy, "apply" for an assignment and "in
 * effect" for a resolved policy, so Baluarte's checks are never confused with the security
 * policies GitLab itself enforces.
 */

import type {
  Person,
  PolicyChange,
  Review,
  Assignment,
  ResolutionOutcome,
  TargetType,
} from "../api/policies";
import type { CurrentUser } from "../auth/session";

export const CAN_WRITE = "policy:write";

export function canWritePolicies(user: CurrentUser | null): boolean {
  return user?.permissions.includes(CAN_WRITE) ?? false;
}

export const targetTypeLabels: Record<TargetType, string> = {
  instance: "Whole instance",
  group: "Group",
  project: "Project",
};

export const outcomeLabels: Record<ResolutionOutcome, string> = {
  effective: "In effect",
  overridden: "Replaced by a closer setting",
  out_of_scope: "Not applied here (outside the baseline's groups)",
};

export const gapReasons: Record<string, string> = {
  not_in_inventory: "not collected by Baluarte yet",
  missing: "no longer found in GitLab",
  inaccessible: "not readable with Baluarte's token",
  cycle: "its parent groups loop",
  too_deep: "nested deeper than GitLab allows",
};

/** Where an assignment applies, in words: "the whole instance", "group platform", … */
export function targetText(
  assignment: Pick<Assignment, "target_type" | "target_path" | "target_id">,
): string {
  if (assignment.target_type === "instance") return "the whole instance";
  const noun = assignment.target_type === "group" ? "group" : "project";
  return assignment.target_path
    ? `${noun} ${assignment.target_path}`
    : `${noun} with GitLab ID ${String(assignment.target_id)} (no longer collected)`;
}

export const GOVERNANCE = "/governance";
export const APPLY_VIEW = "/governance/where-baselines-apply";
export const SECURITY_POLICIES_VIEW = "/governance/gitlab-security-policies";
export const CHANGES_VIEW = "/governance/changes";

/** A person named in a change: their display name, else the recorded actor. */
export function personText(person: Person | null | undefined): string {
  if (!person) return "no one";
  if (person.display_name) return person.display_name;
  if (person.actor === "unknown") return "an unrecorded user";
  return person.actor;
}

/** What a change does, as a sentence fragment: "New version of x (replaces v2)". */
export function changeText(change: PolicyChange): string {
  if (change.kind === "version") {
    const became = change.version !== null ? ` v${String(change.version)}` : "";
    return change.base_version === null
      ? `New baseline ${change.policy_name}${became}`
      : `New version of ${change.policy_name}${became} (replaces v${String(change.base_version)})`;
  }
  const what = `${change.policy_name} v${String(change.version ?? "?")}`;
  const where = targetText({
    target_type: change.target_type ?? "instance",
    target_path: change.target_path,
    target_id: change.target_id ?? 0,
  });
  return change.kind === "assign"
    ? `Apply ${what} to ${where}`
    : `Stop checking ${what} on ${where}`;
}

/** Told to whoever made a change that now waits for review. */
export function proposedMessage(change: PolicyChange): string {
  return `Proposed: ${changeText(change)}. It takes effect when someone else approves it under Governance → Changes.`;
}

/** How a version or assignment in force was reviewed, for its details. */
export function reviewText(review: Review | null | undefined): string {
  if (!review) return "No change record";
  if (review.migrated) return "In force before review was recorded";
  if (review.self_approved) {
    return `Not reviewed: applied by ${personText(review.proposed_by)} while review was off`;
  }
  return `Proposed by ${personText(review.proposed_by)}, approved by ${personText(review.decided_by)}`;
}

export function baselineHref(name: string, version?: number): string {
  const base = `${GOVERNANCE}/baselines/${encodeURIComponent(name)}`;
  return version === undefined ? base : `${base}?version=${String(version)}`;
}

/** The baseline editor: empty, the recommended baseline, or a new version of an existing one. */
export function editorHref(from?: { name: string; version: number } | "recommended"): string {
  const base = `${GOVERNANCE}/baselines/new`;
  if (!from) return base;
  if (from === "recommended") return `${base}?start=recommended`;
  const query = new URLSearchParams({ from: from.name, version: String(from.version) });
  return `${base}?${query.toString()}`;
}

/** Open findings checked against one baseline. */
export function findingsHref(name: string): string {
  return `/findings?${new URLSearchParams({ state: "open", policy: name }).toString()}`;
}

/** Size of ``text`` in UTF-8 bytes, as the API counts it. */
export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}
