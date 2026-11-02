/** Types and calls for policy baselines and assignments (docs/POLICY_ENGINE.md). */

import { ApiError, getJson } from "./client";
import { changeFailure, sendJson, sendJsonWithStatus } from "./send";

// Limits the API enforces (backend/src/baluarte/policy/loader.py and schema.py).
export const MAX_SOURCE_BYTES = 64 * 1024;
export const NAME_PATTERN = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
export const ASSIGNMENTS_PAGE = 100;

export type TargetType = "instance" | "group" | "project";

export interface PolicySummary {
  name: string;
  latest_version: number;
  versions: number;
  latest_effective_at: string;
}

/** Who made or decided a baseline change: ``user:<id>``, a ``system:`` actor, or "unknown". */
export interface Person {
  actor: string;
  user_id: number | null;
  display_name: string | null;
}

/** How a version or assignment in force came to be: the change that applied it. */
export interface Review {
  change_id: number;
  /** Review was required, and someone other than the proposer approved it. */
  reviewed: boolean;
  /** Applied while review was not required; the proposer approved it themselves. */
  self_approved: boolean;
  /** In force before review was recorded. */
  migrated: boolean;
  proposed_by: Person;
  decided_by: Person | null;
  decided_at: string | null;
}

export interface VersionSummary {
  id: number;
  name: string;
  version: number;
  yaml_digest: string;
  effective_at: string;
  created_at: string;
  /** Null only when no change is recorded for it. */
  review?: Review | null;
}

export interface PolicyVersion extends VersionSummary {
  source_yaml: string;
  document: Record<string, unknown>;
}

export interface PolicyError {
  path: string;
  message: string;
}

export interface Validation {
  valid: boolean;
  errors: PolicyError[];
  name: string | null;
  yaml_digest: string | null;
  rule_ids: string[];
}

export interface Assignment {
  id: number;
  policy_id: number;
  policy_name: string;
  version: number;
  yaml_digest: string;
  target_type: TargetType;
  target_id: number;
  /** From the inventory; null when the target is no longer recorded there. */
  target_path: string | null;
  target_state: string | null;
  created_at: string;
  /** Null only when no change is recorded for it. */
  review?: Review | null;
}

export interface AssignmentPage {
  items: Assignment[];
  next_after_id: number | null;
}

export interface AssignmentRequest {
  policy_name: string;
  version: number;
  target_type: TargetType;
  /** The GitLab ID of the group or project; omitted for the instance. */
  target_id?: number;
}

export interface Target {
  target_type: TargetType;
  target_id: number;
  path: string;
  state: string | null;
}

export type ResolutionOutcome = "effective" | "overridden" | "out_of_scope";

export interface Resolved {
  assignment_id: number;
  policy_id: number;
  name: string;
  version: number;
  yaml_digest: string;
  assigned_to: Target;
  outcome: ResolutionOutcome;
}

export interface Resolution {
  target: Target;
  ancestry: "complete" | "incomplete";
  gaps: { gitlab_id: number; reason: string }[];
  /** Nearest first: the target, its groups up to the top level, then the instance. */
  path: Target[];
  policies: Resolved[];
  considered: Resolved[];
  /** Set on a project that evaluation skips; always null for groups. */
  not_evaluated?: "archived" | null;
}

export function listPolicies(signal?: AbortSignal): Promise<PolicySummary[]> {
  return getJson("/api/v1/policies", undefined, signal);
}

export function listVersions(name: string, signal?: AbortSignal): Promise<VersionSummary[]> {
  return getJson(`/api/v1/policies/${encodeURIComponent(name)}/versions`, undefined, signal);
}

export function getVersion(
  name: string,
  version: number,
  signal?: AbortSignal,
): Promise<PolicyVersion> {
  return getJson(
    `/api/v1/policies/${encodeURIComponent(name)}/versions/${String(version)}`,
    undefined,
    signal,
  );
}

export async function validatePolicy(sourceYaml: string, csrfToken: string): Promise<Validation> {
  const result = await sendJson<Validation>(
    "POST",
    "/api/v1/policies/validate",
    { source_yaml: sourceYaml },
    csrfToken,
  );
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export interface Saved {
  version: PolicyVersion;
  /** False when the source was identical to the latest version, which is returned instead. */
  created: boolean;
}

export type ChangeKind = "version" | "assign" | "unassign";
export type ChangeState = "proposed" | "approved" | "rejected";

/** A change to the baselines in force; while review is required it waits for someone else. */
export interface PolicyChange {
  id: number;
  kind: ChangeKind;
  state: ChangeState;
  policy_name: string;
  /** ``version``: the version it became once approved; otherwise the version pinned. */
  version: number | null;
  yaml_digest: string | null;
  /** ``version``: the latest version in force when it was proposed. */
  base_version: number | null;
  target_type: TargetType | null;
  target_id: number | null;
  target_path: string | null;
  assignment_id: number | null;
  review_required: boolean;
  self_approved: boolean;
  migrated: boolean;
  proposed_by: Person;
  proposed_at: string;
  decided_by: Person | null;
  decided_at: string | null;
  decision_note: string | null;
}

export interface PolicyChangeDetail extends PolicyChange {
  source_yaml: string | null;
  base_source_yaml: string | null;
}

export interface PolicyChangePage {
  /** Whether new changes currently need a second person. */
  review_required: boolean;
  items: PolicyChange[];
  next_before_id: number | null;
}

/** A save or assignment that is waiting for someone else to approve it. */
export interface Proposed {
  proposed: PolicyChangeDetail;
}

/**
 * Save the next version. A 422 carries the validation errors in ``detail.errors``, which the
 * generic sender cannot show, so they are returned for the editor to list.
 */
export async function savePolicy(
  sourceYaml: string,
  csrfToken: string,
): Promise<Saved | Proposed | { errors: PolicyError[] }> {
  let response: Response;
  try {
    response = await fetch("/api/v1/policies", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ source_yaml: sourceYaml }),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (response.status === 422) {
    const body = (await response.json().catch(() => null)) as {
      detail?: { errors?: PolicyError[] } | string;
    } | null;
    const detail = body?.detail;
    if (detail && typeof detail === "object" && Array.isArray(detail.errors)) {
      return { errors: detail.errors };
    }
    throw new ApiError(422, changeFailure(422, typeof detail === "string" ? detail : null));
  }
  if (!response.ok) {
    let detail: string | null = null;
    try {
      const body = (await response.json()) as { detail?: unknown };
      detail = typeof body.detail === "string" ? body.detail : null;
    } catch {
      detail = null;
    }
    throw new ApiError(response.status, changeFailure(response.status, detail));
  }
  if (response.status === 202) {
    return { proposed: (await response.json()) as PolicyChangeDetail };
  }
  return {
    version: (await response.json()) as PolicyVersion,
    created: response.status === 201,
  };
}

export function listAssignments(
  afterId: number | null,
  signal?: AbortSignal,
  policyName?: string,
): Promise<AssignmentPage> {
  return getJson(
    "/api/v1/policy-assignments",
    { policy_name: policyName ?? null, after_id: afterId, limit: ASSIGNMENTS_PAGE },
    signal,
  );
}

/** Every assignment, following pages; assignments are few, so one list is enough. */
export async function listAllAssignments(
  signal?: AbortSignal,
  policyName?: string,
): Promise<Assignment[]> {
  const items: Assignment[] = [];
  let after: number | null = null;
  for (;;) {
    const page: AssignmentPage = await listAssignments(after, signal, policyName);
    items.push(...page.items);
    if (page.next_after_id === null) return items;
    after = page.next_after_id;
  }
}

export interface Applied {
  assignment: Assignment;
  /** False when this exact version was already applied to the target, so nothing changed. */
  created: boolean;
}

/** Applies at once, or returns the proposal when review is required. */
export async function assignPolicy(
  request: AssignmentRequest,
  csrfToken: string,
): Promise<Applied | Proposed> {
  const { status, data } = await sendJsonWithStatus<Assignment | PolicyChangeDetail>(
    "POST",
    "/api/v1/policy-assignments",
    request,
    csrfToken,
  );
  if (data === null) throw new ApiError(204, "The API returned no content.");
  if (status === 202) return { proposed: data as PolicyChangeDetail };
  return { assignment: data as Assignment, created: status === 201 };
}

/** Removes at once (null), or returns the proposal when review is required. */
export async function removeAssignment(
  id: number,
  csrfToken: string,
): Promise<PolicyChangeDetail | null> {
  return sendJson<PolicyChangeDetail>(
    "DELETE",
    `/api/v1/policy-assignments/${String(id)}`,
    undefined,
    csrfToken,
  );
}

export const CHANGES_PAGE = 50;

export function listPolicyChanges(
  options: { state?: ChangeState; beforeId?: number | null; limit?: number },
  signal?: AbortSignal,
): Promise<PolicyChangePage> {
  return getJson(
    "/api/v1/policy-changes",
    {
      state: options.state ?? null,
      before_id: options.beforeId ?? null,
      limit: options.limit ?? CHANGES_PAGE,
    },
    signal,
  );
}

export function getPolicyChange(id: number, signal?: AbortSignal): Promise<PolicyChangeDetail> {
  return getJson(`/api/v1/policy-changes/${String(id)}`, undefined, signal);
}

async function decide(
  id: number,
  decision: "approve" | "reject",
  note: string | null,
  csrfToken: string,
): Promise<PolicyChangeDetail> {
  const result = await sendJson<PolicyChangeDetail>(
    "POST",
    `/api/v1/policy-changes/${String(id)}/${decision}`,
    note === null ? {} : { note },
    csrfToken,
  );
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export function approveChange(
  id: number,
  note: string | null,
  csrfToken: string,
): Promise<PolicyChangeDetail> {
  return decide(id, "approve", note, csrfToken);
}

export function rejectChange(
  id: number,
  note: string,
  csrfToken: string,
): Promise<PolicyChangeDetail> {
  return decide(id, "reject", note, csrfToken);
}

export interface GovernanceSettings {
  require_review: boolean;
}

export function getGovernanceSettings(signal?: AbortSignal): Promise<GovernanceSettings> {
  return getJson("/api/v1/settings/governance", undefined, signal);
}

export async function putGovernanceSettings(
  settings: GovernanceSettings,
  csrfToken: string,
): Promise<GovernanceSettings> {
  const result = await sendJson<GovernanceSettings>(
    "PUT",
    "/api/v1/settings/governance",
    settings,
    csrfToken,
  );
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export function effectivePolicies(
  targetType: "group" | "project",
  gitlabId: number,
  signal?: AbortSignal,
): Promise<Resolution> {
  return getJson(
    `/api/v1/gitlab/${targetType}s/${String(gitlabId)}/effective-policies`,
    undefined,
    signal,
  );
}

export interface TreeAssignment {
  assignment_id: number;
  policy_id: number;
  name: string;
  version: number;
  yaml_digest: string;
}

export type EffectStatus = "set_here" | "inherited" | "out_of_scope";

/** The deciding assignment of one baseline at a group or project. */
export interface TreeEffect {
  name: string;
  assignment_id: number;
  policy_id: number;
  version: number;
  yaml_digest: string;
  status: EffectStatus;
  assigned_to: Target;
  /** Farther assignments of the same baseline that this one replaces, nearest first. */
  replaces: Resolved[];
}

export interface TreeNode {
  target: Target;
  /** A group's parent, or a project's namespace group (null for a personal project). */
  parent_gitlab_id: number | null;
  ancestry: "complete" | "incomplete";
  gaps: { gitlab_id: number; reason: string }[];
  /** Assignments made to this target itself. */
  assignments: TreeAssignment[];
  in_effect: TreeEffect[];
}

export interface TreeProject extends TreeNode {
  namespace_gitlab_id: number;
  namespace_kind: string | null;
}

export interface AssignmentTree {
  instance: Target;
  instance_assignments: TreeAssignment[];
  /** Every group, ordered by path. */
  groups: TreeNode[];
  /** Only projects with an assignment of their own. */
  projects: TreeProject[];
}

/** Where baselines apply, resolved by the API for every group at once. */
export function getAssignmentTree(signal?: AbortSignal): Promise<AssignmentTree> {
  return getJson("/api/v1/policy-assignments/tree", undefined, signal);
}
