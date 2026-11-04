/**
 * Words for the control mapping: framework and status labels, rule types, mapping changes, and
 * the checks a change form makes before sending. The API checks every change again.
 */

import {
  personId,
  type ControlStatus,
  type FrameworkId,
  type MappingChange,
  type MappingChangeKind,
  type MappingChangeRequest,
  type MappingChangeState,
} from "../api/controls";
import type { Person } from "../api/policies";
import { personText } from "../policies/format";
import {
  APPROVAL_TYPE,
  BRANCH_TYPE,
  MEMBERS_TYPE,
  PROJECT_APPROVALS_TYPE,
  SCAN_TYPE,
  TOKENS_TYPE,
  TWO_FACTOR_TYPE,
  VISIBILITY_TYPE,
} from "../policies/yaml";

export const CONTROLS = "/controls";

export const frameworkLabels: Record<FrameworkId, string> = {
  soc2: "SOC 2",
  iso27001: "ISO 27001",
  nist_800_53_moderate: "NIST 800-53",
};

export function frameworkLabel(framework: string): string {
  return frameworkLabels[framework as FrameworkId] ?? framework;
}

export const statusLabels: Record<ControlStatus, string> = {
  met: "Met",
  not_met: "Not met",
  unknown: "Unknown",
  no_results: "No results",
  not_covered: "Not covered",
  not_applicable: "Not applicable",
  manual: "Met outside GitLab",
  review_overdue: "Review overdue",
};

export const statusMeaning: Record<ControlStatus, string> = {
  met: "Every result of the mapped rules passes, or is a failure waived by an exception.",
  not_met: "At least one result of a mapped rule fails.",
  unknown:
    "Some results could not be decided, and none fails. Not counted as met until they are known.",
  no_results: "Rules are mapped, but no baseline in force has a rule of a mapped type.",
  not_covered: "No rule type counts toward this control. Map one, or record how it is met.",
  not_applicable: "The organization has recorded that this control is out of scope.",
  manual: "The organization has recorded that this control is met outside GitLab.",
  review_overdue:
    "The organization's record that this control is out of scope or met elsewhere has passed its review date, so it no longer counts.",
};

/** Badge colours; the label always carries the meaning, so colour is never the only cue. */
export const statusClasses: Record<ControlStatus, string> = {
  met: "border-emerald-300 bg-emerald-50 text-emerald-950",
  not_met: "border-red-300 bg-red-50 text-red-950",
  unknown: "border-amber-300 bg-amber-50 text-amber-950",
  no_results: "border-slate-300 bg-slate-50 text-slate-800",
  not_covered: "border-slate-300 bg-white text-slate-800",
  not_applicable: "border-slate-300 bg-slate-100 text-slate-800",
  manual: "border-sky-300 bg-sky-50 text-sky-950",
  review_overdue: "border-orange-300 bg-orange-50 text-orange-950",
};

export const ruleTypes: readonly string[] = [
  VISIBILITY_TYPE,
  BRANCH_TYPE,
  SCAN_TYPE,
  APPROVAL_TYPE,
  PROJECT_APPROVALS_TYPE,
  MEMBERS_TYPE,
  TWO_FACTOR_TYPE,
  TOKENS_TYPE,
];

export const ruleTypeLabels: Record<string, string> = {
  [VISIBILITY_TYPE]: "Project visibility",
  [BRANCH_TYPE]: "Default branch protected",
  [SCAN_TYPE]: "Required security scans",
  [APPROVAL_TYPE]: "Merge request approval policy",
  [PROJECT_APPROVALS_TYPE]: "Project approval settings",
  [MEMBERS_TYPE]: "Privileged members",
  [TWO_FACTOR_TYPE]: "Two-factor authentication required",
  [TOKENS_TYPE]: "Project access tokens",
};

export function ruleTypeLabel(ruleType: string): string {
  return ruleTypeLabels[ruleType] ?? ruleType;
}

export const kindLabels: Record<MappingChangeKind, string> = {
  map: "Map a rule type",
  unmap: "Remove a built-in mapping",
  not_applicable: "Mark not applicable",
  manual: "Record as met outside GitLab",
  withdraw: "Withdraw a change",
};

export const changeStateLabels: Record<MappingChangeState, string> = {
  proposed: "Waiting for review",
  active: "In force",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
};

/** What a mapping change does, as a sentence fragment. */
export function mappingChangeText(change: MappingChange): string {
  const control = `${frameworkLabel(change.framework)} ${change.control_id}`;
  const rule = change.rule_type ? ruleTypeLabel(change.rule_type) : "a rule type";
  switch (change.kind) {
    case "map":
      return `Count ${rule} toward ${control}`;
    case "unmap":
      return `Stop counting ${rule} toward ${control}`;
    case "not_applicable":
      return `Mark ${control} not applicable`;
    case "manual":
      return `Record ${control} as met outside GitLab`;
    case "withdraw":
      return change.supersedes_id === null
        ? `Withdraw an earlier change to ${control}`
        : `Withdraw change ${String(change.supersedes_id)} to ${control}`;
  }
}

/** A proposer or decider in words, whichever shape the API gave. */
export function changePerson(person: Person | number | null): string {
  if (person === null) return "no one";
  if (typeof person === "number") return `user ${String(person)}`;
  return personText(person);
}

export function isOwnChange(change: MappingChange, userId: number | undefined): boolean {
  return userId !== undefined && personId(change.proposed_by) === userId;
}

/** How a decided change was reviewed, in words an auditor can quote. */
export function mappingDecisionText(change: MappingChange): string {
  if (change.state === "proposed") return "Waiting for someone other than the proposer";
  if (change.self_approved) return "Not reviewed: review was off, so it applied at once";
  const by = changePerson(change.decided_by);
  if (change.state === "rejected") return `Rejected by ${by}`;
  return `Approved by ${by}`;
}

export const MIN_JUSTIFICATION = 20;
export const MAX_JUSTIFICATION = 2000;
export const MAX_EVIDENCE_REF = 500;
export const MAX_REVIEW_DAYS = 365;

/** Kinds that need a review date: a control taken out of scope or met elsewhere is re-checked. */
export function needsReviewBy(kind: MappingChangeKind): boolean {
  return kind === "not_applicable" || kind === "manual";
}

export interface ChangeForm {
  kind: MappingChangeKind;
  ruleType: string;
  justification: string;
  evidenceRef: string;
  /** ``YYYY-MM-DD``, as a date input holds it. */
  reviewBy: string;
  supersedesId: number | null;
}

function utcDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** The latest review date allowed: 365 days after ``today``. */
export function latestReviewBy(today: Date): string {
  return utcDay(new Date(today.getTime() + MAX_REVIEW_DAYS * 24 * 60 * 60 * 1000));
}

export type ValidatedChange = { request: MappingChangeRequest } | { errors: string[] };

/** Check a change form the way the API will, and build the request with only its fields. */
export function validateChange(
  framework: FrameworkId,
  controlId: string,
  form: ChangeForm,
  today: Date = new Date(),
): ValidatedChange {
  const errors: string[] = [];
  const request: MappingChangeRequest = {
    framework,
    control_id: controlId,
    kind: form.kind,
    justification: form.justification.trim(),
  };
  if (form.kind === "map" || form.kind === "unmap") {
    if (!form.ruleType) errors.push("Choose a rule type.");
    else request.rule_type = form.ruleType;
  }
  if (form.kind === "withdraw") {
    if (form.supersedesId === null) errors.push("Choose the change to withdraw.");
    else request.supersedes_id = form.supersedesId;
  }
  const justification = request.justification;
  if (justification.length < MIN_JUSTIFICATION) {
    errors.push(
      `Justification: explain the reason in at least ${String(MIN_JUSTIFICATION)} characters.`,
    );
  } else if (justification.length > MAX_JUSTIFICATION) {
    errors.push(`Justification: at most ${String(MAX_JUSTIFICATION)} characters.`);
  }
  const evidence = form.evidenceRef.trim();
  if (evidence.length > MAX_EVIDENCE_REF) {
    errors.push(`Where the evidence is: at most ${String(MAX_EVIDENCE_REF)} characters.`);
  } else if (evidence) {
    request.evidence_ref = evidence;
  } else if (form.kind === "manual") {
    errors.push("Where the evidence is: say where the evidence that the control is met is kept.");
  }
  if (needsReviewBy(form.kind)) {
    const reviewBy = form.reviewBy.trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(reviewBy) || Number.isNaN(Date.parse(reviewBy))) {
      errors.push("Review by: choose the date this decision must be reviewed again.");
    } else if (reviewBy <= utcDay(today)) {
      errors.push("Review by: choose a date after today.");
    } else if (reviewBy > latestReviewBy(today)) {
      errors.push(`Review by: at most ${String(MAX_REVIEW_DAYS)} days ahead.`);
    } else {
      request.review_by = reviewBy;
    }
  }
  return errors.length > 0 ? { errors } : { request };
}
