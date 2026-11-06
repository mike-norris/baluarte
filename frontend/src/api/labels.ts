/**
 * Label consistency (docs/LABELS.md): one label taxonomy for every group, where groups and
 * projects drift from it, and reviewed plans that fix the drift in GitLab. Only an approved plan
 * writes, with a separate write token, and it never deletes a label. A label list Baluarte could
 * not read is unknown, never "no labels".
 */

import { getJson } from "./client";
import { postChange } from "./controls";
import type { Person } from "./policies";
import { sendJson } from "./send";
import type { AsOfAnswer } from "../asof/format";

export type TaxonomyState = "proposed" | "approved" | "rejected" | "superseded";

export interface TaxonomyLabel {
  name: string;
  /** ``#rrggbb``; compared without regard to case. */
  color: string;
  description: string | null;
  /** Names to rename from. */
  aliases: string[];
}

export interface TaxonomySpec {
  scope: { groups: string[] };
  placement: string;
  /** Labels not in the taxonomy count as drift. */
  strict: boolean;
  labels: TaxonomyLabel[];
}

export interface TaxonomyVersion {
  id: number;
  version: number;
  name: string;
  state: TaxonomyState;
  digest: string;
  source_yaml?: string;
  spec: TaxonomySpec | null;
  justification: string | null;
  proposed_by: Person;
  proposed_at: string;
  review_required: boolean;
  self_approved: boolean;
  decided_by: Person | null;
  decided_at: string | null;
  decision_note: string | null;
  in_force_from: string | null;
}

export interface TaxonomyView extends AsOfAnswer {
  current: TaxonomyVersion | null;
  versions: TaxonomyVersion[];
  review_required: boolean;
}

export interface TaxonomyIssue {
  path: string;
  message: string;
}

export interface TaxonomyValidation {
  valid: boolean;
  errors: TaxonomyIssue[];
  spec: TaxonomySpec | null;
}

export type DriftKind =
  | "missing"
  | "misplaced"
  | "misnamed"
  | "color"
  | "description"
  | "duplicate"
  | "unknown_label"
  | "unknown";

export const driftKinds: readonly DriftKind[] = [
  "missing",
  "misplaced",
  "misnamed",
  "color",
  "description",
  "duplicate",
  "unknown_label",
  "unknown",
];

export interface LabelOwner {
  type: "group" | "project";
  gitlab_id: number;
  path: string;
}

export interface LabelValues {
  name?: string | null;
  color?: string | null;
  description?: string | null;
}

export interface ObservationRef {
  id: number;
  payload_digest: string | null;
  collected_at: string;
}

export interface DriftItem {
  /** Stable: ``<owner type>:<gitlab_id>:<kind>:<label or ->``; drift is computed, not stored. */
  id: string;
  owner: LabelOwner;
  top_level_group: string;
  kind: DriftKind;
  label: string | null;
  observed: (LabelValues & { defined_at?: LabelOwner | null }) | null;
  expected: (LabelValues & { placement?: LabelOwner | null }) | null;
  /** Why the labels could not be read, for ``unknown``. */
  reason: string | null;
  observations: ObservationRef[];
}

export interface DriftTotals {
  total: number;
  by_kind: Partial<Record<DriftKind, number>>;
  by_group: { group: string; total: number; by_kind: Partial<Record<DriftKind, number>> }[];
}

export interface DriftPage extends AsOfAnswer {
  items: DriftItem[];
  /** The last item's ``id``, to pass as ``after``; null on the last page. */
  next_after: string | null;
  totals: DriftTotals;
  taxonomy: { id: number; version: number; name: string } | null;
  collected_at: string | null;
}

export interface DriftFilters {
  group?: string;
  kind?: DriftKind;
  label?: string;
}

export interface ObservedLabel {
  id: number;
  name: string;
  color: string;
  description: string | null;
  priority: number | null;
  defined_at: LabelOwner;
}

export interface OwnerLabels extends AsOfAnswer {
  owner: LabelOwner;
  status: "known" | "unknown";
  reason: string | null;
  observation: ObservationRef | null;
  labels: ObservedLabel[];
  drift: DriftItem[];
}

export type LabelPlanState =
  | "draft"
  | "submitted"
  | "approved"
  | "rejected"
  | "applying"
  | "applied"
  | "failed"
  | "expired";

export const labelPlanStates: readonly LabelPlanState[] = [
  "draft",
  "submitted",
  "approved",
  "rejected",
  "applying",
  "applied",
  "failed",
  "expired",
];

export type StepAction = "create" | "rename" | "update";
export type StepStatus = "pending" | "skipped" | "applied" | "failed" | "not_run";

export interface LabelPlanStep {
  step: number;
  owner: LabelOwner;
  action: StepAction;
  label: string;
  prior: LabelValues | null;
  intended: LabelValues;
  idempotency_key: string;
  status: StepStatus;
  request_status: number | null;
  /** Null until read back. */
  verified: boolean | null;
  message: string | null;
  finished_at: string | null;
}

/** Drift a plan reports but never fixes: duplicates, misplaced and unknown labels. */
export interface ManualItem {
  owner: LabelOwner;
  kind: DriftKind;
  label: string | null;
  reason: string;
}

export type ApplyBlock =
  | "label_writes_disabled"
  | "write_token_missing"
  | "not_approved"
  | "expired"
  | "taxonomy_changed";

export interface LabelPlanSummary {
  id: number;
  state: LabelPlanState;
  scope: { groups: string[] };
  taxonomy: { id: number; version: number; name: string } | null;
  steps: number;
  manual: number;
  plan_digest: string;
  created_by: Person;
  created_at: string;
  expires_at: string;
}

export interface LabelPlan extends Omit<LabelPlanSummary, "steps" | "manual"> {
  steps: LabelPlanStep[];
  manual: ManualItem[];
  digest_valid: boolean;
  submitted_at: string | null;
  decided_by: Person | null;
  decided_at: string | null;
  decision_note: string | null;
  review_required: boolean;
  self_approved: boolean;
  apply_started_at: string | null;
  finished_at: string | null;
  failed_step: number | null;
  failure: string | null;
  can_apply: { allowed: boolean; reasons: string[] };
}

export interface LabelPlanPage {
  items: LabelPlanSummary[];
  next_after_id: number | null;
}

export interface WriteTokenStatus {
  set: boolean;
  /** ``BALUARTE_LABEL_WRITES`` for this deployment. */
  writes_enabled: boolean;
  scopes: string[] | null;
  expires_at: string | null;
  token_name: string | null;
  checked_at: string | null;
  check_error: string | null;
}

export const CAN_PLAN = "remediation:plan";
export const CAN_APPROVE = "remediation:approve";
export const CAN_PROPOSE = "policy:write";
export const DRIFT_PAGE = 50;
export const PLANS_PAGE = 50;
export const MAX_JUSTIFICATION = 2000;
export const MAX_NOTE = 2000;

/** Sentences for the API's reasons for refusing a taxonomy or plan change. */
export const labelRefusalReasons: Record<string, string> = {
  invalid_taxonomy: "the taxonomy is not valid",
  own_change: "you proposed this, so someone else must decide it",
  not_proposed: "it was already decided",
  label_writes_disabled: "label writes are turned off for this deployment",
  write_token_missing: "no label write token is set",
  not_approved: "the plan is not approved",
  plan_expired: "the plan has expired",
  expired: "the plan has expired",
  digest_mismatch: "the plan changed after you opened it; reload and review it again",
  not_draft: "the plan was already submitted",
  not_submitted: "the plan is not waiting for review",
  no_taxonomy: "no taxonomy is in force",
  nothing_to_plan: "there is no drift to fix or report in that scope",
  scope_too_large: "the scope covers more than 500 groups and projects; choose fewer groups",
  taxonomy_changed: "the taxonomy in force has changed since this plan was made",
  token_invalid: "the token is empty or too long",
  unknown_group: "a group in the scope is not in the inventory",
};

/** Why a plan can't be applied now, as sentences. */
export const applyBlockText: Record<ApplyBlock, string> = {
  label_writes_disabled:
    "Label writes are turned off for this deployment (BALUARTE_LABEL_WRITES is not true).",
  write_token_missing: "No label write token is set. An administrator can add one in Settings.",
  not_approved: "The plan must be approved before it can be applied.",
  expired: "The plan has expired. Create a new plan from a fresh read.",
  taxonomy_changed:
    "The taxonomy in force has changed since this plan was made. Create a new plan.",
};

/** A sentence for each reason the plan can't be applied; unknown codes read as words. */
export function applyBlocks(reasons: readonly string[]): string[] {
  return reasons.map(
    (code) => applyBlockText[code as ApplyBlock] ?? `${code.replace(/_/g, " ")}.`,
  );
}

export function getTaxonomy(asOf: string | null, signal?: AbortSignal): Promise<TaxonomyView> {
  return getJson<TaxonomyView>("/api/v1/labels/taxonomy", { as_of: asOf }, signal);
}

export function validateTaxonomy(
  sourceYaml: string,
  csrfToken: string,
): Promise<TaxonomyValidation> {
  return postChange(
    "/api/v1/labels/taxonomy/validate",
    { source_yaml: sourceYaml },
    csrfToken,
    labelRefusalReasons,
  );
}

export function proposeTaxonomy(
  sourceYaml: string,
  justification: string,
  csrfToken: string,
): Promise<TaxonomyVersion> {
  return postChange(
    "/api/v1/labels/taxonomy",
    justification.trim()
      ? { source_yaml: sourceYaml, justification: justification.trim() }
      : { source_yaml: sourceYaml },
    csrfToken,
    labelRefusalReasons,
  );
}

export function decideTaxonomy(
  id: number,
  decision: "approve" | "reject",
  note: string,
  csrfToken: string,
): Promise<TaxonomyVersion> {
  const text = note.trim();
  return postChange(
    `/api/v1/labels/taxonomy/${String(id)}/${decision}`,
    text ? { note: text } : {},
    csrfToken,
    labelRefusalReasons,
  );
}

export function listDrift(
  filters: DriftFilters,
  asOf: string | null,
  after: string | null,
  signal?: AbortSignal,
): Promise<DriftPage> {
  return getJson<DriftPage>(
    "/api/v1/labels/drift",
    { ...filters, as_of: asOf, after, limit: DRIFT_PAGE },
    signal,
  );
}

export function getOwnerLabels(
  type: LabelOwner["type"],
  gitlabId: number,
  asOf: string | null,
  signal?: AbortSignal,
): Promise<OwnerLabels> {
  return getJson<OwnerLabels>(
    `/api/v1/labels/owners/${type}/${String(gitlabId)}`,
    { as_of: asOf },
    signal,
  );
}

export function listLabelPlans(
  state: LabelPlanState | undefined,
  afterId: number | null,
  signal?: AbortSignal,
): Promise<LabelPlanPage> {
  return getJson<LabelPlanPage>(
    "/api/v1/labels/plans",
    { state, after_id: afterId, limit: PLANS_PAGE },
    signal,
  );
}

export function getLabelPlan(id: number, signal?: AbortSignal): Promise<LabelPlan> {
  return getJson<LabelPlan>(`/api/v1/labels/plans/${String(id)}`, {}, signal);
}

export function createLabelPlan(groups: string[], csrfToken: string): Promise<LabelPlan> {
  return postChange(
    "/api/v1/labels/plans",
    groups.length > 0 ? { scope: { groups } } : {},
    csrfToken,
    labelRefusalReasons,
  );
}

export function planAction(
  id: number,
  action: "submit" | "apply",
  csrfToken: string,
): Promise<LabelPlan> {
  return postChange(
    `/api/v1/labels/plans/${String(id)}/${action}`,
    {},
    csrfToken,
    labelRefusalReasons,
  );
}

/** Approval names the digest the approver reviewed; the API refuses any other. */
export function approveLabelPlan(
  id: number,
  planDigest: string,
  note: string,
  csrfToken: string,
): Promise<LabelPlan> {
  const text = note.trim();
  return postChange(
    `/api/v1/labels/plans/${String(id)}/approve`,
    text ? { plan_digest: planDigest, note: text } : { plan_digest: planDigest },
    csrfToken,
    labelRefusalReasons,
  );
}

export function rejectLabelPlan(id: number, note: string, csrfToken: string): Promise<LabelPlan> {
  return postChange(
    `/api/v1/labels/plans/${String(id)}/reject`,
    { note: note.trim() },
    csrfToken,
    labelRefusalReasons,
  );
}

export function getWriteToken(signal?: AbortSignal): Promise<WriteTokenStatus> {
  return getJson<WriteTokenStatus>("/api/v1/settings/label-write-token", {}, signal);
}

export async function putWriteToken(token: string, csrfToken: string): Promise<WriteTokenStatus> {
  const status = await sendJson<WriteTokenStatus>(
    "PUT",
    "/api/v1/settings/label-write-token",
    { token },
    csrfToken,
  );
  if (status === null) throw new Error("The API returned no content.");
  return status;
}

export async function removeWriteToken(csrfToken: string): Promise<void> {
  await sendJson("DELETE", "/api/v1/settings/label-write-token", undefined, csrfToken);
}
