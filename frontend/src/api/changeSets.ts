/** Template change sets and saved selections (roadmap M14.3). */

import { ApiError, getJson } from "./client";
import { sendJson } from "./send";
import type { ProjectRef, UserRef } from "./templates";

export type DriftFilter = "drift" | "unknown" | "aligned";

export interface Selection {
  groups?: string[];
  projects?: string[];
  tags?: string[];
  template?: string | null;
  drift?: DriftFilter[];
  findings?: "open" | "none" | null;
  policy?: string | null;
}

export interface SelectionPreview {
  projects: number;
  listed: ProjectRef[];
  truncated: boolean;
}

export interface SavedSelection {
  id: number;
  name: string;
  selection: Selection;
  created_at: string;
}

export type WarningCode = "weakens_protection" | "removes_access" | "disrupts_work";

export interface Change {
  key: string;
  before: unknown;
  after: unknown;
  observed_at: string | null;
  warnings: { code: WarningCode; text: string }[];
}

export type SkipCode =
  "unsupported" | "unknown" | "excepted" | "conflicts_with_baseline" | "label_plans";

export interface ChangeTarget {
  project: ProjectRef;
  changes: Change[];
  skipped: { key: string; code: SkipCode; reason: string }[];
  unchanged: string[];
}

export interface ChangeSetDocument {
  format: number;
  connection: { id: number; provider: string };
  selection: Selection;
  source: {
    template: string | null;
    template_id: number | null;
    settings: Record<string, unknown>;
  };
  targets: ChangeTarget[];
}

export type ChangeSetState = "proposed" | "approved" | "rejected" | "withdrawn" | "expired";

export interface ChangeSetSummary {
  id: number;
  connection_id: number;
  title: string;
  state: ChangeSetState;
  digest: string;
  targets: number;
  changes: number;
  skipped: number;
  sensitive: boolean;
  approvals_needed: number;
  source: string | null;
  created_by: UserRef;
  created_at: string;
  expires_at: string;
  first_approved_by: UserRef | null;
  first_approved_at: string | null;
  first_approval_note: string | null;
  decided_by: UserRef | null;
  decided_at: string | null;
  decision_note: string | null;
  development_decision: boolean;
  ended_at: string | null;
}

export interface ChangeSet extends ChangeSetSummary {
  document: ChangeSetDocument;
}

export interface ChangeSetRequest {
  title: string;
  selection: Selection;
  template?: string;
  settings?: Record<string, unknown>;
  expires_in_hours: number;
}

async function send<T>(method: "POST" | "DELETE", path: string, body: unknown, csrf: string) {
  const result = await sendJson<T>(method, path, body, csrf);
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export function previewSelection(selection: Selection, csrf: string): Promise<SelectionPreview> {
  return send("POST", "/api/v1/template-selections/preview", selection, csrf);
}

export function listSavedSelections(
  signal?: AbortSignal,
): Promise<{ selections: SavedSelection[] }> {
  return getJson("/api/v1/template-selections", undefined, signal);
}

export function saveSelection(
  name: string,
  selection: Selection,
  csrf: string,
): Promise<SavedSelection> {
  return send("POST", "/api/v1/template-selections", { name, selection }, csrf);
}

export async function deleteSavedSelection(id: number, csrf: string): Promise<void> {
  await sendJson("DELETE", `/api/v1/template-selections/${String(id)}`, undefined, csrf);
}

export function listChangeSets(signal?: AbortSignal): Promise<{ change_sets: ChangeSetSummary[] }> {
  return getJson("/api/v1/template-change-sets", undefined, signal);
}

export function getChangeSet(id: number, signal?: AbortSignal): Promise<ChangeSet> {
  return getJson(`/api/v1/template-change-sets/${String(id)}`, undefined, signal);
}

export function createChangeSet(request: ChangeSetRequest, csrf: string): Promise<ChangeSet> {
  return send("POST", "/api/v1/template-change-sets", request, csrf);
}

export function decideChangeSet(
  id: number,
  action: "approve" | "reject" | "withdraw",
  body: { digest?: string; note?: string },
  csrf: string,
): Promise<ChangeSet> {
  return send("POST", `/api/v1/template-change-sets/${String(id)}/${action}`, body, csrf);
}
