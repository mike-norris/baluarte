/** Types and calls for the GitLab inventory endpoints (backend/README.md). */

import { getJson } from "./client";

export type InventoryState = "active" | "missing" | "inaccessible";
export type Visibility = "private" | "internal" | "public";
export type Freshness = "failed" | "never" | "stale" | "partial" | "fresh";
export type CollectionKind = "capabilities" | "groups" | "projects";

interface Tracked {
  id: number;
  gitlab_id: number;
  name: string;
  visibility: Visibility | null;
  state: InventoryState;
  first_seen_at: string;
  last_seen_at: string;
  last_error: string | null;
  last_error_at: string | null;
}

export interface Group extends Tracked {
  parent_gitlab_id: number | null;
  full_path: string;
}

export interface Project extends Tracked {
  namespace_gitlab_id: number;
  path_with_namespace: string;
  archived: boolean;
  default_branch: string | null;
}

export interface Page<T> {
  items: T[];
  next_after_id: number | null;
}

export interface Run {
  id: number;
  kind: CollectionKind;
  status: "running" | "succeeded" | "partial" | "failed";
  started_at: string;
  finished_at: string | null;
  seen: number;
  error: string | null;
}

export interface KindFreshness {
  status: Freshness;
  running: boolean;
  last_run: Run | null;
  last_success_at: string | null;
}

export interface ResourceCounts {
  active: number;
  missing: number;
  inaccessible: number;
  with_errors: number;
}

export interface FreshnessReport {
  status: Freshness | "not_configured";
  checked_at: string;
  stale_after_minutes: number;
  sync_interval_minutes: number | null;
  collections: Partial<Record<CollectionKind, KindFreshness>>;
  resources: Partial<Record<"groups" | "projects", ResourceCounts>>;
}

export interface InventoryFilters {
  search: string;
  state: InventoryState | "";
  visibility: Visibility | "";
  archived: "true" | "false" | "";
  hasError: boolean;
}

function common(filters: InventoryFilters, afterId: number | null) {
  return {
    search: filters.search.trim(),
    state: filters.state,
    has_error: filters.hasError ? true : null,
    after_id: afterId,
    limit: 100,
  };
}

export function getFreshness(signal?: AbortSignal): Promise<FreshnessReport> {
  return getJson("/api/v1/gitlab/freshness", undefined, signal);
}

export function listProjects(
  filters: InventoryFilters,
  afterId: number | null,
  signal?: AbortSignal,
): Promise<Page<Project>> {
  return getJson(
    "/api/v1/gitlab/projects",
    { ...common(filters, afterId), visibility: filters.visibility, archived: filters.archived },
    signal,
  );
}

export function listGroups(
  filters: InventoryFilters,
  afterId: number | null,
  signal?: AbortSignal,
): Promise<Page<Group>> {
  return getJson("/api/v1/gitlab/groups", common(filters, afterId), signal);
}
