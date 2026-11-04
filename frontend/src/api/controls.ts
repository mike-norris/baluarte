/**
 * Client for the control mapping: framework controls, the rules mapped to them, their posture,
 * and the organization's changes to the built-in mapping (docs/CONTROL_MAPPING.md).
 */

import { ApiError, getJson } from "./client";
import { changeFailure, detailText } from "./send";
import type { Person } from "./policies";

export type FrameworkId = "soc2" | "iso27001" | "nist_800_53_moderate";
export const frameworkIds: readonly FrameworkId[] = ["soc2", "iso27001", "nist_800_53_moderate"];

export type ControlStatus =
  | "met"
  | "not_met"
  | "unknown"
  | "no_results"
  | "not_covered"
  | "not_applicable"
  | "manual"
  | "review_overdue";
export const controlStatuses: readonly ControlStatus[] = [
  "not_met",
  "review_overdue",
  "unknown",
  "no_results",
  "not_covered",
  "met",
  "manual",
  "not_applicable",
];

export interface Framework {
  id: FrameworkId;
  name: string;
}

/** A rule type counted toward a control: built in, or added by an organization change. */
export interface MappedRule {
  rule_type: string;
  source: "default" | "organization";
  /** The active change that added it; null for a default pair. */
  change_id: number | null;
  /** Why the default mapping links them. */
  rationale?: string | null;
  /** Why the organization linked them. */
  justification?: string | null;
}

/** The active change that marks a control not applicable or met outside GitLab. */
export interface ControlChangeRef {
  id: number;
  kind: "not_applicable" | "manual";
  justification: string;
  evidence_ref: string | null;
  review_by: string | null;
}

export interface ControlCounts {
  pass: number;
  fail: number;
  unknown: number;
  excepted: number;
  projects: number;
}

/** A built-in pair the organization removed, with the active change that removed it. */
export interface UnmappedRule {
  rule_type: string;
  change_id: number;
  justification: string;
}

export interface Control {
  framework: FrameworkId;
  id: string;
  title: string;
  area: string;
  status: ControlStatus;
  mapped: MappedRule[];
  /** Absent from older responses. */
  unmapped?: UnmappedRule[];
  change: ControlChangeRef | null;
  counts: ControlCounts;
}

export interface ControlList {
  mapping_version: string;
  frameworks: Framework[];
  controls: Control[];
}

export type MappingChangeKind = "map" | "unmap" | "not_applicable" | "manual" | "withdraw";
export type MappingChangeState = "proposed" | "active" | "rejected" | "withdrawn";

export interface MappingChange {
  id: number;
  framework: FrameworkId;
  control_id: string;
  kind: MappingChangeKind;
  rule_type: string | null;
  justification: string;
  evidence_ref: string | null;
  review_by: string | null;
  supersedes_id: number | null;
  state: MappingChangeState;
  /** Who proposed or decided it; some responses give only the user ID. */
  proposed_by: Person | number;
  proposed_at: string;
  decided_by: Person | number | null;
  decided_at: string | null;
  decision_note: string | null;
  self_approved: boolean;
  mapping_version: string;
}

/** The rules behind a control's results: a policy's rule of a mapped type, with its counts. */
export interface ControlRule {
  /** The policy name, or the policy version reference. */
  policy: string | { name: string; version?: number };
  rule_id: string;
  rule_type: string;
  counts: ControlCounts;
}

export interface ControlDetail extends Control {
  rules: ControlRule[];
  changes: MappingChange[];
}

export interface MappingChangeList {
  items: MappingChange[];
  /** Whether new changes need a second person, when the API reports it. */
  review_required?: boolean;
}

export interface MappingChangeRequest {
  framework: FrameworkId;
  control_id: string;
  kind: MappingChangeKind;
  rule_type?: string;
  justification: string;
  evidence_ref?: string;
  review_by?: string;
  supersedes_id?: number;
}

export function policyName(policy: ControlRule["policy"]): string {
  return typeof policy === "string" ? policy : policy.name;
}

/** The user ID behind a proposer or decider, whichever shape the API gave. */
export function personId(person: Person | number | null): number | null {
  if (person === null) return null;
  return typeof person === "number" ? person : person.user_id;
}

export function controlPath(framework: string, controlId: string): string {
  return `/controls/${encodeURIComponent(framework)}/${encodeURIComponent(controlId)}`;
}

export function listControls(framework: FrameworkId, signal?: AbortSignal): Promise<ControlList> {
  return getJson("/api/v1/controls", { framework }, signal);
}

export function getControl(
  framework: string,
  controlId: string,
  signal?: AbortSignal,
): Promise<ControlDetail> {
  return getJson(`/api/v1${controlPath(framework, controlId)}`, undefined, signal);
}

export async function listMappingChanges(
  state: MappingChangeState | null,
  signal?: AbortSignal,
): Promise<MappingChangeList> {
  const body = await getJson<MappingChangeList | MappingChange[]>(
    "/api/v1/control-mapping/changes",
    { state },
    signal,
  );
  return Array.isArray(body) ? { items: body } : body;
}

/** Sentences for the API's reasons for refusing a mapping change (409). */
export const refusalReasons: Record<string, string> = {
  control_unknown: "the control is not in the catalogue",
  unknown_control: "the control is not in the catalogue",
  rule_type_unknown: "the rule type does not exist",
  unknown_rule_type: "the rule type does not exist",
  duplicate: "the same change is already in force or waiting for review",
  duplicate_change: "the same change is already in force or waiting for review",
  already_mapped: "that rule type already counts toward this control",
  not_default: "only a pair from the built-in mapping can be removed",
  not_a_default_pair: "only a pair from the built-in mapping can be removed",
  not_mapped: "that rule type does not count toward this control",
  status_exists: "the control is already marked not applicable or met outside GitLab",
  control_status_exists: "the control is already marked not applicable or met outside GitLab",
  review_by_required: "a review date is required",
  review_by_past: "the review date must be in the future",
  review_by_too_far: "the review date must be at most 365 days ahead",
  supersedes_required: "choose the change to withdraw",
  supersedes_not_active: "the change to withdraw is no longer in force",
  supersedes_other_control: "the change to withdraw belongs to another control",
  supersede_pending: "another change to withdraw or renew it is already waiting for review",
  evidence_ref_required: "say where the evidence is kept",
  not_proposed: "the change was already decided",
  own_change: "you proposed this change, so someone else must decide it",
};

/**
 * The API's explanation of a refusal: a plain message, validation issues, or reason codes
 * (``code``, ``reasons`` or ``codes``) turned into sentences from ``reasons``.
 */
export function refusalText(
  detail: unknown,
  reasons: Record<string, string> = refusalReasons,
): string | null {
  const reasonSentence = (code: string) => reasons[code] ?? code.replace(/_/g, " ");
  if (typeof detail === "string") {
    return /^[a-z]+(_[a-z]+)+$/.test(detail) ? reasonSentence(detail) : detail;
  }
  if (Array.isArray(detail)) {
    if (detail.every((item) => typeof item === "string")) {
      return detail.length > 0 ? (detail as string[]).map(reasonSentence).join("; ") : null;
    }
    return detailText(detail);
  }
  if (typeof detail === "object" && detail !== null) {
    const record = detail as Record<string, unknown>;
    const codes = [record.reasons, record.codes].find(Array.isArray) as unknown[] | undefined;
    if (codes && codes.length > 0) {
      return codes
        .map((item) =>
          typeof item === "string"
            ? reasonSentence(item)
            : typeof item === "object" && item !== null && "code" in item
              ? reasonSentence(String((item as { code: unknown }).code))
              : null,
        )
        .filter((text): text is string => text !== null)
        .join("; ");
    }
    if (typeof record.code === "string") return reasonSentence(record.code);
    if (typeof record.message === "string") return record.message;
  }
  return null;
}

/** POST a change with the CSRF token; a refusal's reason codes become sentences from ``reasons``. */
export async function postChange<T>(
  path: string,
  body: unknown,
  csrfToken: string,
  reasons: Record<string, string> = refusalReasons,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail ?? null;
    } catch {
      detail = null;
    }
    throw new ApiError(
      response.status,
      changeFailure(response.status, refusalText(detail, reasons)),
    );
  }
  return (await response.json()) as T;
}

export function proposeMappingChange(
  request: MappingChangeRequest,
  csrfToken: string,
): Promise<MappingChange> {
  return postChange("/api/v1/control-mapping/changes", request, csrfToken);
}

export function decideMappingChange(
  id: number,
  decision: "approve" | "reject",
  note: string | null,
  csrfToken: string,
): Promise<MappingChange> {
  return postChange(
    `/api/v1/control-mapping/changes/${String(id)}/${decision}`,
    note === null ? {} : { note },
    csrfToken,
  );
}
