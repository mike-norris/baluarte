import type { InventoryState, Visibility } from "../api/gitlab";

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
