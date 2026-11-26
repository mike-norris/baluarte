/** Release practices, where they apply and what releases show (docs/RELEASE_GOVERNANCE.md). */

import { getJson } from "./client";
import type { Target, TargetType } from "./policies";
import { postJson, sendJson } from "./send";

export type Preset = "merge_request" | "release_tag" | "release_branch" | "custom";
export type ProductionFrom = "default_branch" | "tag" | "branch";
export type PracticeStatus = "follows" | "differs" | "unknown" | "no_releases" | "unassigned";

export interface PracticeSettings {
  production_from: ProductionFrom;
  /** Tag or branch patterns production deploys, GitLab wildcards. */
  ref_patterns: string[];
  environments: string[];
  required_approvals: number;
  creator_may_approve: boolean;
  deployer_may_approve: boolean;
  require_merge_request_review: boolean;
}

export interface PresetInfo {
  preset: Preset;
  label: string;
  /** The preset's defaults; null for custom. */
  settings: PracticeSettings | null;
}

export interface ReleasePractice {
  name: string;
  preset: Preset;
  preset_label: string;
  description: string | null;
  settings: PracticeSettings;
  assignments: number;
  created_by: string;
  created_at: string;
  updated_by: string;
  updated_at: string;
}

export interface PracticeList {
  presets: PresetInfo[];
  practices: ReleasePractice[];
  window_days: number;
}

export interface PracticeAssignment {
  id: number;
  connection_id: number;
  name: string;
  target: Target;
  assigned_by: string;
  created_at: string;
}

export interface Observed {
  deployments: number;
  by_kind: Record<string, number>;
  expected: number;
  other: number;
  unknown: number;
  other_refs: string[];
  last_deployed_at: string | null;
}

export interface ProjectPractice {
  project_id: number;
  gitlab_id: number;
  path: string;
  default_branch: string | null;
  practice: string | null;
  preset: Preset | null;
  decided_by: Target | null;
  status: PracticeStatus;
  observed: Observed;
}

export interface ProjectList {
  window_days: number;
  counts: Partial<Record<PracticeStatus, number>>;
  projects: ProjectPractice[];
}

export function listPractices(signal?: AbortSignal): Promise<PracticeList> {
  return getJson("/api/v1/release-practices", undefined, signal);
}

export function listPracticeAssignments(
  signal?: AbortSignal,
): Promise<{ assignments: PracticeAssignment[] }> {
  return getJson("/api/v1/release-practice-assignments", undefined, signal);
}

export function listPracticeProjects(signal?: AbortSignal): Promise<ProjectList> {
  return getJson("/api/v1/release-practices/projects", undefined, signal);
}

export function createPractice(
  request: {
    name: string;
    preset: Preset;
    description: string | null;
    settings: Partial<PracticeSettings>;
  },
  csrfToken: string,
): Promise<ReleasePractice> {
  return postJson("/api/v1/release-practices", request, csrfToken);
}

export async function updatePractice(
  name: string,
  request: { description: string | null; settings: Partial<PracticeSettings> },
  csrfToken: string,
): Promise<void> {
  await sendJson(
    "PUT",
    `/api/v1/release-practices/${encodeURIComponent(name)}`,
    request,
    csrfToken,
  );
}

export async function deletePractice(name: string, csrfToken: string): Promise<void> {
  await sendJson(
    "DELETE",
    `/api/v1/release-practices/${encodeURIComponent(name)}`,
    undefined,
    csrfToken,
  );
}

export function assignPractice(
  request: { name: string; target_type: TargetType; target_id?: number },
  csrfToken: string,
): Promise<PracticeAssignment> {
  return postJson("/api/v1/release-practice-assignments", request, csrfToken);
}

export async function unassignPractice(id: number, csrfToken: string): Promise<void> {
  await sendJson(
    "DELETE",
    `/api/v1/release-practice-assignments/${String(id)}`,
    undefined,
    csrfToken,
  );
}
