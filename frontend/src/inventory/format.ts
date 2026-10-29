import type { Freshness, InventoryState, Visibility } from "../api/gitlab";

const timeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** Timestamps are shown in UTC, matching the API and the evidence records. */
export function formatTime(iso: string | null): string {
  return iso ? `${timeFormat.format(new Date(iso))} UTC` : "Never";
}

export function visibilityLabel(visibility: Visibility | null): string {
  if (visibility === null) return "Unknown";
  return visibility.charAt(0).toUpperCase() + visibility.slice(1);
}

export const stateLabels: Record<InventoryState, string> = {
  active: "Active",
  inaccessible: "Inaccessible",
  missing: "Missing",
};

export const stateHelp: Record<InventoryState, string> = {
  active: "Returned by the last collection.",
  inaccessible: "Exists, but the GitLab token may not read it.",
  missing: "Deleted, or hidden from the GitLab token. GitLab does not say which.",
};

export const freshnessLabels: Record<Freshness, string> = {
  failed: "Failed",
  never: "Never collected",
  stale: "Stale",
  partial: "Partial",
  fresh: "Up to date",
};

/** Colour always accompanies text, never replaces it. */
export const freshnessTone: Record<Freshness, string> = {
  failed: "border-red-300 bg-red-50 text-red-900",
  never: "border-slate-300 bg-slate-100 text-slate-800",
  stale: "border-amber-300 bg-amber-50 text-amber-900",
  partial: "border-amber-300 bg-amber-50 text-amber-900",
  fresh: "border-emerald-300 bg-emerald-50 text-emerald-900",
};
