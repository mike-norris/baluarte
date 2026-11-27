/** Evidence items people add: uploads and references (roadmap M15.2). */

import { ApiError, getJson } from "./client";
import { changeFailure, detailMessage, readDetail, sendJson } from "./send";
import { scopeHeaders } from "./workspaceChoice";

export const ITEMS = "/api/v1/evidence-items";
export const MAX_FILE_BYTES = 25 * 1024 * 1024;

export type EvidenceKind = "file" | "reference";
export type EvidenceState = "draft" | "submitted" | "accepted" | "rejected";
export type Freshness = "fresh" | "stale" | "unknown";

export interface UserRef {
  user_id: number;
  display_name: string | null;
}

export interface ControlLink {
  framework: string;
  version: string;
  control_id: string;
}

export interface EvidenceItem {
  id: number;
  kind: EvidenceKind;
  title: string;
  description: string;
  owner: UserRef;
  period_start: string | null;
  period_end: string | null;
  valid_until: string | null;
  freshness: Freshness;
  restricted: boolean;
  state: EvidenceState;
  controls: ControlLink[];
  supersedes_id: number | null;
  superseded_by_id: number | null;
  filename: string | null;
  content_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
  file_removed_at: string | null;
  url: string | null;
  check_status: "reachable" | "unreachable" | null;
  checked_at: string | null;
  check_http_status: number | null;
  check_sha256: string | null;
  added_by: UserRef;
  added_at: string;
  submitted_at: string | null;
  reviewed_by: UserRef | null;
  reviewed_at: string | null;
  review_note: string | null;
  development_review: boolean;
}

export interface EvidenceItemsPage {
  items: EvidenceItem[];
  checks_enabled: boolean;
}

export interface ControlInput {
  framework: string;
  control_id: string;
}

export interface EvidenceItemInput {
  kind: EvidenceKind;
  title: string;
  description: string;
  valid_until: string | null;
  period_start: string | null;
  period_end: string | null;
  restricted: boolean;
  controls: ControlInput[];
  url?: string;
  supersedes_id?: number;
}

export function listEvidenceItems(
  filters: { state?: EvidenceState | null; control?: string | null },
  signal?: AbortSignal,
): Promise<EvidenceItemsPage> {
  return getJson(ITEMS, { state: filters.state ?? null, control: filters.control ?? null }, signal);
}

export function getEvidenceItem(id: number, signal?: AbortSignal): Promise<EvidenceItem> {
  return getJson(`${ITEMS}/${String(id)}`, {}, signal);
}

async function required<T>(result: Promise<T | null>): Promise<T> {
  const value = await result;
  if (value === null) throw new ApiError(204, "The API returned no content.");
  return value;
}

export function addEvidenceItem(input: EvidenceItemInput, csrf: string): Promise<EvidenceItem> {
  return required(sendJson<EvidenceItem>("POST", ITEMS, input, csrf));
}

export function evidenceAction(
  id: number,
  action: "submit" | "accept" | "reject" | "check",
  csrf: string,
  note?: string,
): Promise<EvidenceItem> {
  const body = action === "accept" || action === "reject" ? { note: note ?? null } : undefined;
  return required(sendJson<EvidenceItem>("POST", `${ITEMS}/${String(id)}/${action}`, body, csrf));
}

export async function deleteEvidenceItem(id: number, csrf: string): Promise<void> {
  await sendJson("DELETE", `${ITEMS}/${String(id)}`, undefined, csrf);
}

/** Upload a draft file item's file as the raw request body. */
export async function uploadEvidenceFile(
  id: number,
  file: File,
  csrf: string,
): Promise<EvidenceItem> {
  if (file.size > MAX_FILE_BYTES) throw new ApiError(413, "A file is at most 25 MiB.");
  let response: Response;
  try {
    response = await fetch(
      `${ITEMS}/${String(id)}/file?filename=${encodeURIComponent(file.name)}`,
      {
        method: "PUT",
        credentials: "same-origin",
        headers: {
          ...scopeHeaders(),
          Accept: "application/json",
          "Content-Type": "application/octet-stream",
          "X-CSRF-Token": csrf,
        },
        body: file,
      },
    );
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) {
    const detail = await readDetail(response);
    throw new ApiError(response.status, changeFailure(response.status, detailMessage(detail)));
  }
  return (await response.json()) as EvidenceItem;
}

export function fileHref(id: number): string {
  return `${ITEMS}/${String(id)}/file`;
}

export const stateLabels: Record<EvidenceState, string> = {
  draft: "Draft",
  submitted: "Waiting for review",
  accepted: "Accepted",
  rejected: "Rejected",
};

export const freshnessLabels: Record<Freshness, string> = {
  fresh: "Fresh",
  stale: "Stale",
  unknown: "No freshness date",
};
