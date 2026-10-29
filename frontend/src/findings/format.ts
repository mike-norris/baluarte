import type { FindingState, Outcome, UnknownReason } from "../api/evaluations";

export const outcomeLabels: Record<Outcome, string> = {
  pass: "Pass",
  fail: "Fail",
  unknown: "Unknown",
  unsupported: "Unsupported",
  excepted: "Excepted",
};

export const outcomeMeaning: Record<Outcome, string> = {
  pass: "The collected value meets the rule.",
  fail: "The collected value breaks the rule.",
  unknown:
    "Could not be decided: data missing, stale, unreadable or incomplete. Not counted as passing.",
  unsupported: "This GitLab instance cannot report the setting. Not counted as passing.",
  excepted: "Covered by an approved exception.",
};

export const severityLabels: Record<string, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

export const findingStateLabels: Record<FindingState, string> = {
  open: "Open",
  resolved: "Resolved",
  excepted: "Excepted",
};

export const resolutionLabels: Record<string, string> = {
  passed: "the rule passed",
  no_longer_applies: "the rule no longer applies to this project",
};

/** Why a rule could not be decided (docs/EVALUATION.md); the API lists them in rank order. */
export const reasonHelp: Record<UnknownReason, string> = {
  target_not_in_inventory: "The project is not in the inventory.",
  target_missing:
    "The project was not found by the last discovery: deleted, or hidden from the GitLab token.",
  target_inaccessible: "The project exists, but the GitLab token may not read it.",
  collection_not_configured: "GitLab is not configured, so nothing was collected.",
  collection_failed: "The latest collection failed, so the inventory may be out of date.",
  collection_never: "No collection has completed yet.",
  collection_stale: "The collected data is older than the freshness limit.",
  target_collection_error: "Collecting this project's data reported an error.",
  ancestry_incomplete:
    "Some groups above the project could not be followed, so a nearer policy version may apply.",
  capability_unknown: "Whether this GitLab instance supports the setting has not been detected.",
  value_unknown: "GitLab did not report the value.",
  evidence_missing: "No collected evidence backs the value that was judged.",
  evidence_digest_mismatch:
    "The evidence record's SHA-256 digest no longer matches its contents: " +
    "it may have been altered.",
  evidence_mismatch: "The evidence record does not hold the value that was judged.",
};

export const capabilityLabels: Record<string, string> = {
  not_required: "Not required",
  supported: "Supported",
  unsupported: "Not supported by this GitLab instance",
  unknown: "Not detected",
};

function isTextList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

/** True when a value needs a formatted JSON block rather than a line of text. */
export function isStructured(value: unknown): boolean {
  return typeof value === "object" && value !== null && !isTextList(value);
}

/** Render a stored JSON value as text; objects become formatted JSON. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (isTextList(value)) return value.length ? value.join(", ") : "None";
  return JSON.stringify(value, null, 2);
}
