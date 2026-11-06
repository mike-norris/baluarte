/**
 * Client for the manager space: results rolled up by group or tag, trends over time, and the
 * groups assigned to each manager (which limit the time per person they may see).
 */

import { ApiError, getJson } from "./client";
import type { FrameworkId } from "./controls";
import type { Person } from "./policies";
import { sendJson } from "./send";
import type { AsOfAnswer } from "../asof/format";

export interface ScopeGroup {
  gitlab_id: number;
  path: string;
}

export interface ManagerScope {
  id: number;
  user: Person;
  group: ScopeGroup;
  assigned_by: Person;
  assigned_at: string;
  ended_by: Person | null;
  ended_at: string | null;
}

export interface MyScopes {
  /** True for administrators, who see every group. */
  all: boolean;
  items: ManagerScope[];
}

export interface AppUser {
  user_id: number;
  display_name: string | null;
  email: string | null;
  roles: string[];
}

export interface SeverityCounts {
  critical: number;
  high: number;
  medium: number;
  low: number;
  total: number;
}

export interface ChangeWindow {
  total: number;
  reviewed: number;
  direct_pushes: number;
  unknown: number;
  /** Reviewed out of all, 0 to 1; null when there were no changes. */
  rate: number | null;
}

export type RowKey =
  | { type: "group"; gitlab_id: number; path: string }
  | { type: "tag"; key: string; value: string };

export interface OverviewRow {
  key: RowKey;
  projects: { monitored: number; excluded: number };
  findings_open: SeverityCounts;
  controls_not_met: Partial<Record<FrameworkId, number>>;
  changes: { days_30: ChangeWindow; days_90: ChangeWindow };
  /** Null when no label taxonomy is in force. */
  label_drift: number | null;
  /** Null when time has never been collected. */
  time_30d: { spent_seconds: number; estimate_seconds: number | null } | null;
}

export interface OverviewParent {
  type: "instance" | "group" | "tag";
  path?: string;
  key?: string;
  value?: string;
}

export interface Overview extends AsOfAnswer {
  /** Figures shown as of now even when viewing the past, e.g. ``time`` and ``tags``. */
  current_only: string[];
  parent: OverviewParent;
  rows: OverviewRow[];
}

export type Bucket = "week" | "month";

export interface TrendPoint {
  /** The end of the bucket. */
  end: string;
  findings_open: SeverityCounts;
  /** 0 to 1, or null when nothing was evaluated. */
  rule_pass_rate: number | null;
  change_review_rate: number | null;
  time_spent_seconds: number | null;
}

export interface Trends {
  bucket: Bucket;
  from: string;
  to: string;
  points: TrendPoint[];
}

/** Where the overview and trends are rolled up: everything, one group, or one tag. */
export interface Focus {
  group?: string;
  /** ``key:value``. */
  tag?: string;
}

export const MAX_BUCKETS: Record<Bucket, number> = { week: 105, month: 36 };

export function getOverview(
  focus: Focus,
  signal?: AbortSignal,
  asOf: string | null = null,
): Promise<Overview> {
  return getJson(
    "/api/v1/manager/overview",
    { group: focus.group, tag: focus.tag, as_of: asOf },
    signal,
  );
}

export function getTrends(
  focus: Focus,
  bucket: Bucket,
  range: { from?: string | undefined; to?: string | undefined },
  signal?: AbortSignal,
): Promise<Trends> {
  return getJson(
    "/api/v1/manager/trends",
    { group: focus.group, tag: focus.tag, bucket, from: range.from, to: range.to },
    signal,
  );
}

export function listScopes(
  state: "active" | "all",
  signal?: AbortSignal,
): Promise<{ items: ManagerScope[] }> {
  return getJson("/api/v1/manager/scopes", { state }, signal);
}

export function myScopes(signal?: AbortSignal): Promise<MyScopes> {
  return getJson("/api/v1/manager/scopes/me", undefined, signal);
}

export function listUsers(signal?: AbortSignal): Promise<{ items: AppUser[] }> {
  return getJson("/api/v1/manager/users", undefined, signal);
}

export const scopeRefusalWords: Record<string, string> = {
  user_unknown: "that user has not signed in to Baluarte",
  group_unknown: "a chosen group is not in the inventory",
  already_assigned: "a chosen group is already assigned to that user",
  scope_unknown: "that assignment no longer exists",
  not_active: "the assignment has already ended",
};

export async function assignScopes(
  userId: number,
  groupIds: number[],
  csrfToken: string,
): Promise<{ items: ManagerScope[] }> {
  const result = await sendJson<{ items: ManagerScope[] }>(
    "POST",
    "/api/v1/manager/scopes",
    { user_id: userId, group_gitlab_ids: groupIds },
    csrfToken,
    scopeRefusalWords,
  );
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export async function endScope(id: number, csrfToken: string): Promise<ManagerScope> {
  const result = await sendJson<ManagerScope>(
    "POST",
    `/api/v1/manager/scopes/${String(id)}/end`,
    {},
    csrfToken,
    scopeRefusalWords,
  );
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

/** A row's name: the group path or ``key=value``. */
export function rowName(key: RowKey): string {
  return key.type === "group" ? key.path : `${key.key}=${key.value}`;
}

/** A share as a whole percentage, or a dash when there is nothing to divide. */
export function percent(rate: number | null): string {
  return rate === null ? "–" : `${String(Math.round(rate * 100))}%`;
}

/** A person by name, falling back to the actor. */
export function personName(person: Person): string {
  return person.display_name ?? person.actor;
}

export function userName(user: AppUser): string {
  return user.display_name ?? user.email ?? `User ${String(user.user_id)}`;
}
