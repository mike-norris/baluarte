import type { InventoryFilters, InventoryState, Visibility } from "../api/gitlab";

export type InventoryView = "projects" | "groups";

const states: readonly InventoryState[] = ["active", "missing", "inaccessible"];
const visibilities: readonly Visibility[] = ["private", "internal", "public"];

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | "" {
  return allowed.find((item) => item === value) ?? "";
}

/** Filters live in the URL so a filtered view can be bookmarked and shared. */
export function readFilters(params: URLSearchParams): {
  view: InventoryView;
  filters: InventoryFilters;
} {
  return {
    view: params.get("view") === "groups" ? "groups" : "projects",
    filters: {
      search: params.get("search") ?? "",
      state: oneOf(params.get("state"), states),
      visibility: oneOf(params.get("visibility"), visibilities),
      archived: oneOf(params.get("archived"), ["true", "false"] as const),
      hasError: params.get("has_error") === "true",
    },
  };
}

export function writeFilters(view: InventoryView, filters: InventoryFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (view === "groups") params.set("view", "groups");
  if (filters.search.trim()) params.set("search", filters.search.trim());
  if (filters.state) params.set("state", filters.state);
  if (view === "projects" && filters.visibility) params.set("visibility", filters.visibility);
  if (view === "projects" && filters.archived) params.set("archived", filters.archived);
  if (filters.hasError) params.set("has_error", "true");
  return params;
}

export function isFiltered(filters: InventoryFilters): boolean {
  return Boolean(
    filters.search.trim() ||
      filters.state ||
      filters.visibility ||
      filters.archived ||
      filters.hasError,
  );
}
