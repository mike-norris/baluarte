import type { FreshnessReport } from "../api/gitlab";

/**
 * Every status a freshness signal can show. ``unknown`` means the status could not be read,
 * which is never treated as current; ``checking`` is the first load only.
 */
export type SignalStatus = FreshnessReport["status"] | "unknown" | "checking";

/** Worst first; the overall status of several signals is the first one present. */
const severity: readonly SignalStatus[] = [
  "failed",
  "unknown",
  "never",
  "stale",
  "not_configured",
  "partial",
  "checking",
  "fresh",
];

export function worst(statuses: readonly SignalStatus[]): SignalStatus {
  return statuses.reduce<SignalStatus>(
    (current, status) => (severity.indexOf(status) < severity.indexOf(current) ? status : current),
    "fresh",
  );
}

export const freshnessLabels: Record<SignalStatus, string> = {
  failed: "Failed",
  never: "Never collected",
  stale: "Stale",
  partial: "Partial",
  fresh: "Up to date",
  not_configured: "Not configured",
  unknown: "Status unknown",
  checking: "Checking…",
};

/** Colour always accompanies text, never replaces it. */
export const freshnessTone: Record<SignalStatus, string> = {
  failed: "border-red-300 bg-red-50 text-red-900",
  unknown: "border-red-300 bg-red-50 text-red-900",
  never: "border-slate-300 bg-slate-100 text-slate-800",
  not_configured: "border-slate-300 bg-slate-100 text-slate-800",
  checking: "border-slate-300 bg-white text-slate-700",
  stale: "border-amber-300 bg-amber-50 text-amber-900",
  partial: "border-amber-300 bg-amber-50 text-amber-900",
  fresh: "border-emerald-300 bg-emerald-50 text-emerald-900",
};

/** Statuses under which GitLab-derived data must not be read as current. */
export function needsAttention(status: SignalStatus): boolean {
  return status === "failed" || status === "stale" || status === "unknown";
}
