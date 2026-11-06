/**
 * Client for time tracking: GitLab time logs and estimates, summed by group, project, label,
 * milestone, week or tag. Figures per person need their own permission and the manager's scope,
 * and every read of them is audited. Time is never a finding or evidence of a control.
 */

import { ApiError, getJson, withQuery } from "./client";
import { changeFailure, detailMessage, readDetail, sendJson } from "./send";

export type TimeGrouping = "group" | "project" | "label" | "milestone" | "week" | `tag:${string}`;

export interface TimeFilters {
  from: string;
  to: string;
  group?: string;
  project?: number;
  /** ``key:value``. */
  tag?: string;
  label?: string;
  milestone?: string;
}

export interface TimeRow {
  key: string;
  label: string;
  spent_seconds: number;
  /** Null when no issue in the row has an estimate. */
  estimate_seconds: number | null;
  issues: number;
  entries: number;
}

export interface TimeTotals {
  spent_seconds: number;
  estimate_seconds: number | null;
  issues: number;
  entries: number;
}

/** ``to`` is exclusive: the day after the last day asked for. */
export interface Period {
  from: string;
  to: string;
}

export interface TimeSummary {
  period: Period;
  by: string;
  rows: TimeRow[];
  totals: TimeTotals;
  /** Whether the deployment shows figures per person at all. */
  per_person_enabled: boolean;
  collected_at: string | null;
}

export interface PersonRow {
  username: string;
  spent_seconds: number;
  issues: number;
  entries: number;
}

export interface PeopleSummary {
  period: Period;
  rows: PersonRow[];
  totals: TimeTotals;
  /** True when the reader may see every project, as administrators do. */
  all_projects: boolean;
  collected_at: string | null;
}

export interface TimeEntry {
  /** GitLab's global ID of the time log. */
  id: string;
  project: { gitlab_id: number; path: string };
  spent_at: string;
  seconds: number;
  issue_iid: number | null;
  merge_request_iid: number | null;
  labels: string[];
  milestone: string | null;
  /** When the entry was no longer returned by GitLab. */
  removed_at: string | null;
}

export interface PersonEntries {
  username: string;
  period: Period;
  items: TimeEntry[];
  next_after: string | null;
}

export interface TimeSettings {
  lookback_days: number;
  reread_days: number;
  /** Null: follows the evidence retention period. */
  retention_days: number | null;
  /** Set by the deployment; read-only here. */
  per_person_enabled: boolean;
}

export type TimeSettingsUpdate = Omit<TimeSettings, "per_person_enabled">;

export const TIME_LIMITS = {
  lookback: { min: 1, max: 730 },
  reread: { min: 1, max: 90 },
  retention: { min: 30, max: 3650 },
} as const;

/** Why figures per person were refused. */
export type PeopleRefusal = "per_person_disabled" | "out_of_scope" | "forbidden";

export class PeopleRefused extends ApiError {
  readonly code: PeopleRefusal;

  constructor(code: PeopleRefusal, message: string) {
    super(403, message);
    this.name = "PeopleRefused";
    this.code = code;
  }
}

export const peopleRefusalText: Record<PeopleRefusal, string> = {
  per_person_disabled: "This deployment does not show time per person.",
  out_of_scope:
    "Time per person is shown only for the groups assigned to you. Ask an administrator to assign the group.",
  forbidden: "You do not have permission to see time per person.",
};

function params(filters: TimeFilters) {
  return {
    from: filters.from,
    to: filters.to,
    group: filters.group,
    project: filters.project,
    tag: filters.tag,
    label: filters.label,
    milestone: filters.milestone,
  };
}

export function getTimeSummary(
  filters: TimeFilters,
  by: TimeGrouping,
  signal?: AbortSignal,
): Promise<TimeSummary> {
  return getJson("/api/v1/time/summary", { ...params(filters), by }, signal);
}

/** A per-person read; a 403 keeps its reason, so the page can say why. */
async function getPeopleJson<T>(
  path: string,
  query: Record<string, string | number | undefined | null>,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(withQuery(path, query), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: signal ?? null,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(0, "The API could not be reached.");
  }
  if (response.status === 403) {
    const detail = await readDetail(response);
    const code =
      typeof detail === "object" && detail !== null && !Array.isArray(detail)
        ? (detail as { code?: unknown }).code
        : null;
    const refusal: PeopleRefusal =
      code === "per_person_disabled" || code === "out_of_scope" ? code : "forbidden";
    throw new PeopleRefused(refusal, peopleRefusalText[refusal]);
  }
  if (!response.ok) {
    const detail = await readDetail(response);
    throw new ApiError(response.status, changeFailure(response.status, detailMessage(detail)));
  }
  return (await response.json()) as T;
}

export function getPeople(filters: TimeFilters, signal?: AbortSignal): Promise<PeopleSummary> {
  return getPeopleJson("/api/v1/time/people", { ...params(filters), by: "person" }, signal);
}

export function getPersonEntries(
  username: string,
  range: { from: string; to: string },
  after: string | null,
  signal?: AbortSignal,
): Promise<PersonEntries> {
  return getPeopleJson(
    `/api/v1/time/people/${encodeURIComponent(username)}`,
    { from: range.from, to: range.to, after },
    signal,
  );
}

export function getTimeSettings(signal?: AbortSignal): Promise<TimeSettings> {
  return getJson("/api/v1/settings/time", undefined, signal);
}

export async function saveTimeSettings(
  settings: TimeSettingsUpdate,
  csrfToken: string,
): Promise<TimeSettings> {
  const saved = await sendJson<TimeSettings>("PUT", "/api/v1/settings/time", settings, csrfToken);
  if (saved === null) throw new ApiError(204, "The API returned no content.");
  return saved;
}

/** Seconds as hours and minutes, e.g. ``12h 30m``; ``0h`` for none. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "No estimate";
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${String(rest)}m`;
  return rest === 0 ? `${String(hours)}h` : `${String(hours)}h ${String(rest)}m`;
}

/** Spent against estimate in words: over, under, or no estimate. */
export function againstEstimate(spent: number, estimate: number | null): string {
  if (estimate === null || estimate === 0) return "No estimate";
  const ratio = Math.round((spent / estimate) * 100);
  return `${String(ratio)}% of estimate`;
}

/** A ``YYYY-MM-DD`` date in UTC, ``days`` before ``now``. */
export function daysAgo(days: number, now: Date = new Date()): string {
  return new Date(now.getTime() - days * 86_400_000).toISOString().slice(0, 10);
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** A valid ``YYYY-MM-DD`` value, or null. */
export function parseDate(value: string | null): string | null {
  const text = value?.trim() ?? "";
  return DATE.test(text) && !Number.isNaN(Date.parse(`${text}T00:00:00Z`)) ? text : null;
}

/** Problems with a period, or null when it can be read. */
export function periodProblem(from: string, to: string): string | null {
  if (parseDate(from) === null || parseDate(to) === null) return "Enter both dates.";
  if (from > to) return "The start must be on or before the end.";
  return null;
}
