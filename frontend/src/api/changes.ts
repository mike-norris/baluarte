/**
 * Change evidence: merge requests merged into a project's default branch and direct pushes to it,
 * each with what was read about its review, pipeline and deployment (docs/CHANGE_EVIDENCE.md).
 * Anything Baluarte could not read is unknown, never passed.
 */

import { getJson } from "./client";
import type { ProjectRef } from "./evaluations";
import type { AsOfAnswer } from "../asof/format";

/** A deployment is a successful production deployment, listed only when asked for. */
export type ChangeKind = "merge_request" | "direct_push" | "deployment";
export type ChangeOutcome = "pass" | "fail" | "unknown";

export const changeKinds: readonly ChangeKind[] = ["merge_request", "direct_push", "deployment"];
export const changeOutcomes: readonly ChangeOutcome[] = ["fail", "unknown", "pass"];

export interface Approver {
  username: string;
  approved_at: string | null;
}

export type Approvals =
  | {
      status: "known";
      approved_by: Approver[];
      /** Reported by GitLab Premium and above; null when not reported. */
      required: number | null;
      left: number | null;
      /** Null when it could not be told. */
      self_approved: boolean | null;
      committer_approved: boolean | null;
    }
  | { status: "unknown"; reason: string | null };

export interface TestSummary {
  total: number;
  success: number;
  failed: number;
  skipped: number;
  error: number;
}

export type ChangePipeline =
  | {
      status: "known";
      id: number;
      pipeline_status: string;
      source: string | null;
      finished_at: string | null;
      /** Null when the pipeline has no test report. */
      tests: TestSummary | null;
    }
  | { status: "none" }
  | { status: "unknown"; reason: string | null };

export interface Deployment {
  environment: string;
  status: string;
  finished_at: string | null;
}

export type Deployments =
  { status: "known"; items: Deployment[] } | { status: "unknown"; reason: string | null };

export interface Change {
  id: number;
  project: ProjectRef;
  kind: ChangeKind;
  /** The merge request's IID; null for a direct push. */
  iid: number | null;
  sha: string;
  title: string | null;
  /** The merge request's or commit's path in GitLab, without the host. */
  web_path: string | null;
  author: string | null;
  merged_by: string | null;
  /** When it was merged, or committed for a direct push. */
  changed_at: string;
  outcome: ChangeOutcome;
  reasons: string[];
  observation: {
    id: number;
    payload_digest: string | null;
    collected_at: string;
  } | null;
  /** Each can be missing from a response; a missing part is unknown. */
  approvals?: Approvals | null;
  pipeline?: ChangePipeline | null;
  deployments?: Deployments | null;
  /** False when a part could not be read this time and will be read again. */
  complete?: boolean;
}

/** One read of the change, kept as evidence; a later read adds a version. */
export interface ChangeVersion {
  observation_id: number;
  collected_at: string;
  source: string;
  payload_digest: string | null;
  digest_valid: boolean;
  payload: Record<string, unknown> | null;
}

export interface ChangeDetail extends Change, AsOfAnswer {
  /** Newest first. */
  versions: ChangeVersion[];
}

export interface ChangePage extends AsOfAnswer {
  items: Change[];
  next_before_id: number | null;
}

export interface ChangeFilters {
  project?: number;
  kind?: ChangeKind;
  /** UTC instants, ``2026-10-01T00:00:00Z``. */
  from?: string;
  to?: string;
  outcome?: ChangeOutcome;
}

export const CHANGES = "/changes";
export const CHANGES_PAGE = 50;

export function changeHref(id: number): string {
  return `${CHANGES}/${String(id)}`;
}

export function changesHref(filters: ChangeFilters): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `${CHANGES}?${text}` : CHANGES;
}

export function listChanges(
  filters: ChangeFilters,
  beforeId: number | null,
  asOf: string | null,
  signal?: AbortSignal,
): Promise<ChangePage> {
  return getJson(
    "/api/v1/changes",
    { ...filters, before_id: beforeId, limit: CHANGES_PAGE, as_of: asOf },
    signal,
  );
}

export function getChange(
  id: number,
  asOf: string | null,
  signal?: AbortSignal,
): Promise<ChangeDetail> {
  return getJson(`/api/v1/changes/${String(id)}`, { as_of: asOf }, signal);
}
