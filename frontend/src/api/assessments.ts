/** Control assessments: a person's conclusion about a control, citing evidence and approved by
 * someone else (roadmap M15.3). */

import { ApiError, getJson } from "./client";
import { sendJson } from "./send";

export const ASSESSMENTS = "/api/v1/assessments";

export type Conclusion = "effective" | "partially_effective" | "not_effective" | "not_tested";
export type AssessmentState = "draft" | "submitted" | "approved" | "rejected";
export type CitationKind =
  "evidence_item" | "observation" | "change_record" | "change_set" | "control_status";

export interface UserRef {
  user_id: number;
  display_name: string | null;
}

export interface Citation {
  id: number;
  kind: CitationKind;
  target_id: number | null;
  title: string;
  snapshot: Record<string, unknown> | null;
  digest: string | null;
  rationale: string;
  cited_at: string;
}

export interface Assessment {
  id: number;
  control: { framework: string; version: string; control_id: string; title: string };
  period_start: string;
  period_end: string;
  conclusion: Conclusion;
  narrative: string;
  state: AssessmentState;
  supersedes_id: number | null;
  superseded_by_id: number | null;
  assessor: UserRef;
  created_at: string;
  submitted_at: string | null;
  reviewed_by: UserRef | null;
  reviewed_at: string | null;
  review_note: string | null;
  development_review: boolean;
  citations: Citation[];
}

export interface AssessmentInput {
  framework: string;
  control_id: string;
  period_start: string;
  period_end: string;
  conclusion: Conclusion;
  narrative: string;
  supersedes_id?: number;
}

export function listAssessments(
  filters: { state?: AssessmentState | null; control?: string | null },
  signal?: AbortSignal,
): Promise<{ items: Assessment[] }> {
  return getJson(
    ASSESSMENTS,
    { state: filters.state ?? null, control: filters.control ?? null },
    signal,
  );
}

export function getAssessment(id: number, signal?: AbortSignal): Promise<Assessment> {
  return getJson(`${ASSESSMENTS}/${String(id)}`, {}, signal);
}

async function required<T>(result: Promise<T | null>): Promise<T> {
  const value = await result;
  if (value === null) throw new ApiError(204, "The API returned no content.");
  return value;
}

export function startAssessment(input: AssessmentInput, csrf: string): Promise<Assessment> {
  return required(sendJson<Assessment>("POST", ASSESSMENTS, input, csrf));
}

export function citeEvidence(
  id: number,
  citation: { kind: CitationKind; target_id: number | null; rationale: string },
  csrf: string,
): Promise<Assessment> {
  return required(
    sendJson<Assessment>("POST", `${ASSESSMENTS}/${String(id)}/citations`, citation, csrf),
  );
}

export function removeCitation(id: number, citationId: number, csrf: string): Promise<Assessment> {
  return required(
    sendJson<Assessment>(
      "DELETE",
      `${ASSESSMENTS}/${String(id)}/citations/${String(citationId)}`,
      undefined,
      csrf,
    ),
  );
}

export function assessmentAction(
  id: number,
  action: "submit" | "approve" | "reject",
  csrf: string,
  note?: string,
): Promise<Assessment> {
  const body = action === "submit" ? undefined : { note: note ?? null };
  return required(
    sendJson<Assessment>("POST", `${ASSESSMENTS}/${String(id)}/${action}`, body, csrf),
  );
}

export async function deleteAssessment(id: number, csrf: string): Promise<void> {
  await sendJson("DELETE", `${ASSESSMENTS}/${String(id)}`, undefined, csrf);
}

export const conclusionLabels: Record<Conclusion, string> = {
  effective: "Effective",
  partially_effective: "Partially effective",
  not_effective: "Not effective",
  not_tested: "Not tested",
};

export const assessmentStateLabels: Record<AssessmentState, string> = {
  draft: "Draft",
  submitted: "Waiting for review",
  approved: "Approved",
  rejected: "Rejected",
};

export const citationKindLabels: Record<CitationKind, string> = {
  evidence_item: "Evidence item",
  observation: "Observation",
  change_record: "Change",
  change_set: "Template change set",
  control_status: "Automated status now",
};
