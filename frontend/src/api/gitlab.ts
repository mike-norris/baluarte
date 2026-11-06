/** Types and calls for the GitLab inventory endpoints (backend/README.md). */

import { getJson } from "./client";
import type { Monitoring } from "./scope";
import type { ItemTag } from "./tags";
import type { AsOfAnswer } from "../asof/format";

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
  /** Whether it is excluded from monitoring, and how; absent from older servers. */
  monitoring?: Monitoring;
  /** Baluarte tags, its own and inherited from groups; absent from older servers. */
  tags?: ItemTag[];
}

export interface Group extends Tracked {
  parent_gitlab_id: number | null;
  full_path: string;
}

export interface Project extends Tracked {
  namespace_gitlab_id: number;
  /** Null for projects discovered before namespace kinds were recorded. */
  namespace_kind: "group" | "user" | null;
  path_with_namespace: string;
  archived: boolean;
  default_branch: string | null;
}

export interface Page<T> {
  items: T[];
  next_after_id: number | null;
  /** How many excluded from monitoring were left out, when they are hidden. */
  excluded_hidden?: number;
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
  /** Finished runs since the last success that failed, and when the first of them started. */
  consecutive_failures?: number;
  failing_since?: string | null;
}

/** Collection is unhealthy: failing repeatedly, or overdue on its schedule. */
export interface CollectionAlert {
  reason: "failing" | "overdue";
  kinds: CollectionKind[];
  consecutive_failures: number;
  failing_since: string | null;
  last_success_at: string | null;
  last_error: string | null;
  message: string;
}

/** Which groups and projects the inventory can cover: what the token's user can see. */
export interface Population {
  basis: "instance" | "token_visibility" | "unknown";
  token_username: string | null;
  is_admin: boolean | null;
  admin_access?: boolean | null;
  statement: string;
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
  alert?: CollectionAlert | null;
  population?: Population | null;
}

export interface InventoryFilters {
  search: string;
  state: InventoryState | "";
  visibility: Visibility | "";
  archived: "true" | "false" | "";
  hasError: boolean;
  /** Show projects and groups excluded from monitoring; the API hides them by default. */
  showExcluded: boolean;
  /** A Baluarte tag as ``key:value``, or empty for any. */
  tag: string;
}

function common(filters: InventoryFilters, afterId: number | null) {
  return {
    search: filters.search.trim(),
    state: filters.state,
    has_error: filters.hasError ? true : null,
    monitoring: filters.showExcluded ? "all" : null,
    tag: filters.tag,
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

/** A group or project's groups, nearest first; ``incomplete`` when one can't be followed. */
export interface Ancestry {
  groups: { gitlab_id: number; full_path: string; state: InventoryState }[];
  status: "complete" | "incomplete";
  gaps: { gitlab_id: number; reason: string }[];
}

export interface GroupDetail {
  group: Group;
  ancestry: Ancestry;
  /** Direct subgroups and projects, personal projects excluded. */
  subgroups: number;
  projects: number;
  collection: KindFreshness;
}

export interface ProjectDetail extends AsOfAnswer {
  project: Project;
  ancestry: Ancestry;
  collection: KindFreshness;
  /** Why baselines are not evaluated for this project, or null when they are. */
  not_evaluated: "archived" | "excluded" | null;
}

export function getGroup(gitlabId: number, signal?: AbortSignal): Promise<GroupDetail> {
  return getJson(`/api/v1/gitlab/groups/${String(gitlabId)}`, undefined, signal);
}

export function getProject(
  gitlabId: number,
  signal?: AbortSignal,
  asOf: string | null = null,
): Promise<ProjectDetail> {
  return getJson(`/api/v1/gitlab/projects/${String(gitlabId)}`, { as_of: asOf }, signal);
}

export function groupHref(gitlabId: number): string {
  return `/inventory/groups/${String(gitlabId)}`;
}

export function projectHref(gitlabId: number): string {
  return `/inventory/projects/${String(gitlabId)}`;
}
