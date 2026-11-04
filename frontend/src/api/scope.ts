/**
 * Client for the monitoring scope: projects, groups and personal namespaces taken out of
 * monitoring with a justification, and included again. Excluded projects stay in the inventory;
 * they are not evaluated, read in detail or counted.
 */

import { getJson } from "./client";
import { postChange } from "./controls";
import type { Person } from "./policies";

export type ExclusionTargetType = "project" | "group" | "personal_namespaces";
export type ExclusionState = "proposed" | "active" | "rejected" | "ended";

/** How a project or group is excluded, on inventory rows and details. */
export interface Monitoring {
  excluded: boolean;
  exclusion_id: number | null;
  via: "project" | "group" | "personal_namespaces" | null;
  /** The covering target's path: the project, the group, or null for personal namespaces. */
  path: string | null;
}

export interface Exclusion {
  id: number;
  target_type: ExclusionTargetType;
  /** The GitLab ID; null for personal namespaces. */
  target_id: number | null;
  target_path: string | null;
  batch_id: string;
  justification: string;
  state: ExclusionState;
  proposed_by: Person | number;
  proposed_at: string;
  review_required: boolean;
  self_approved: boolean;
  decided_by: Person | number | null;
  decided_at: string | null;
  decision_note: string | null;
  ended_by?: Person | number | null;
  ended_at?: string | null;
  end_justification?: string | null;
}

export interface ExclusionList {
  items: Exclusion[];
  review_required?: boolean;
}

export interface ExclusionTarget {
  type: "project" | "group";
  gitlab_id: number;
}

export interface ExclusionRequest {
  targets: ExclusionTarget[];
  personal_namespaces: boolean;
  justification: string;
}

export interface ExclusionBatch {
  batch_id: string;
  items: Exclusion[];
}

export const MIN_JUSTIFICATION = 20;
export const MAX_JUSTIFICATION = 2000;
export const MAX_BATCH = 500;

/** Sentences for the API's reasons for refusing a scope change (409). */
export const scopeRefusalReasons: Record<string, string> = {
  unknown_target: "a selected project or group is not in the inventory",
  target_unknown: "a selected project or group is not in the inventory",
  already_excluded: "a selected project or group is already excluded or waiting for review",
  duplicate: "a selected project or group is already excluded or waiting for review",
  too_many_targets: `at most ${String(MAX_BATCH)} can be excluded at once`,
  batch_too_large: `at most ${String(MAX_BATCH)} can be excluded at once`,
  no_targets: "choose at least one project or group",
  not_active: "the exclusion has already ended",
  not_proposed: "the exclusion was already decided",
  own_change: "you proposed this exclusion, so someone else must decide it",
};

export function listExclusions(
  state: ExclusionState | null,
  signal?: AbortSignal,
): Promise<ExclusionList> {
  return getJson<ExclusionList | Exclusion[]>("/api/v1/scope/exclusions", { state }, signal).then(
    (body) => (Array.isArray(body) ? { items: body } : body),
  );
}

export function proposeExclusions(
  request: ExclusionRequest,
  csrfToken: string,
): Promise<ExclusionBatch> {
  return postChange("/api/v1/scope/exclusions", request, csrfToken, scopeRefusalReasons);
}

export function decideBatch(
  batchId: string,
  decision: "approve" | "reject",
  note: string | null,
  csrfToken: string,
): Promise<ExclusionBatch> {
  return postChange(
    `/api/v1/scope/exclusions/batches/${encodeURIComponent(batchId)}/${decision}`,
    note === null ? {} : { note },
    csrfToken,
    scopeRefusalReasons,
  );
}

export function endExclusions(
  ids: number[],
  justification: string,
  csrfToken: string,
): Promise<unknown> {
  return postChange(
    "/api/v1/scope/exclusions/end",
    { ids, justification },
    csrfToken,
    scopeRefusalReasons,
  );
}

/** The justification's problem, or null when it can be sent. */
export function justificationProblem(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed.length < MIN_JUSTIFICATION) {
    return `Explain the reason in at least ${String(MIN_JUSTIFICATION)} characters.`;
  }
  if (trimmed.length > MAX_JUSTIFICATION) {
    return `Keep it to at most ${String(MAX_JUSTIFICATION)} characters.`;
  }
  return null;
}

/** How a project or group is excluded, in words. */
export function monitoringText(monitoring: Monitoring | undefined | null): string | null {
  if (!monitoring?.excluded) return null;
  if (monitoring.via === "group") return `Excluded with group ${monitoring.path ?? "(unknown)"}`;
  if (monitoring.via === "personal_namespaces") return "Excluded: personal namespace";
  return "Excluded directly";
}

/**
 * Whether "Include again" on this row would work: only an exclusion of this very project or
 * group ends here. One covering it from a group or the personal-namespace setting ends there.
 */
export function includableHere(
  monitoring: Monitoring | undefined | null,
  type: "project" | "group",
  path: string,
): boolean {
  return (
    monitoring?.excluded === true &&
    monitoring.exclusion_id !== null &&
    monitoring.via === type &&
    (monitoring.path === null || monitoring.path === path)
  );
}

/** How a row is excluded, naming the group when it is not this one. */
export function monitoringLabel(
  monitoring: Monitoring | undefined | null,
  type: "project" | "group",
  path: string,
): string | null {
  if (!monitoring?.excluded) return null;
  if (includableHere(monitoring, type, path)) return "Excluded directly";
  return monitoringText(monitoring);
}

/** A target in words: the path, or what a personal-namespaces exclusion covers. */
export function targetText(exclusion: Exclusion): string {
  if (exclusion.target_type === "personal_namespaces") return "All personal-namespace projects";
  const noun = exclusion.target_type === "group" ? "Group" : "Project";
  return `${noun} ${exclusion.target_path ?? String(exclusion.target_id)}`;
}
