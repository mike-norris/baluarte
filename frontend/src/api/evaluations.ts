/** Types and calls for stored evaluation results and findings (backend/README.md, BAL-021). */

import { getJson } from "./client";

export type Outcome = "pass" | "fail" | "unknown" | "unsupported" | "excepted";
export type FindingState = "open" | "resolved" | "excepted";
export type Severity = "low" | "medium" | "high" | "critical";

export const outcomes: readonly Outcome[] = ["pass", "fail", "unknown", "unsupported", "excepted"];
export const severities: readonly Severity[] = ["critical", "high", "medium", "low"];

export function isSeverity(value: string): value is Severity {
  return (severities as readonly string[]).includes(value);
}

export interface EvaluationRun {
  id: number;
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  projects: number;
  /** ``outcomes`` counts every rule result in the run; ``findings`` counts transitions. */
  counts: {
    outcomes?: Partial<Record<Outcome, number>>;
    findings?: Record<string, number>;
  };
  error: string | null;
}

export interface EvaluationFreshness {
  status: "not_configured" | "failed" | "never" | "stale" | "fresh";
  checked_at: string;
  stale_after_minutes: number;
  running: boolean;
  queued: boolean;
  /** A project discovery finished after the latest successful evaluation started. */
  behind_collection: boolean;
  last_run: EvaluationRun | null;
  last_success_at: string | null;
}

export interface ProjectRef {
  gitlab_id: number;
  path: string;
  state: string;
}

export interface PolicyVersionRef {
  policy_id: number;
  name: string;
  version: number;
}

export interface Finding {
  id: number;
  project: ProjectRef;
  policy: PolicyVersionRef;
  rule_id: string;
  severity: string;
  state: FindingState;
  resolution: string | null;
  latest_outcome: Outcome;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
}

interface FindingPage {
  items: Finding[];
  next_after_id: number | null;
}

interface RunPage {
  items: EvaluationRun[];
  next_before_id: number | null;
}

/** Filters the findings API accepts; the Findings page reads the same query parameters. */
export interface FindingFilters {
  state?: FindingState;
  outcome?: Outcome;
  severity?: Severity;
  project?: number;
  policy?: string;
  rule?: string;
}

export function getEvaluationFreshness(signal: AbortSignal): Promise<EvaluationFreshness> {
  return getJson<EvaluationFreshness>("/api/v1/evaluations/freshness", {}, signal);
}

/** The most recent successful run within the latest ``limit`` runs, or null. */
export async function latestSuccessfulRun(
  signal: AbortSignal,
  limit = 100,
): Promise<{ run: EvaluationRun | null; newest: EvaluationRun | null }> {
  const page = await getJson<RunPage>("/api/v1/evaluations/runs", { limit }, signal);
  return {
    run: page.items.find((run) => run.status === "succeeded") ?? null,
    newest: page.items[0] ?? null,
  };
}

export const FINDINGS_PAGE = 200;
export const FINDINGS_MAX_PAGES = 5;

/**
 * Findings matching ``filters``, following ``next_after_id`` for at most ``maxPages`` pages.
 * ``truncated`` is true when more remain, so totals must be read as "at least".
 */
export async function collectFindings(
  filters: FindingFilters,
  signal: AbortSignal,
  maxPages = FINDINGS_MAX_PAGES,
): Promise<{ items: Finding[]; truncated: boolean }> {
  const items: Finding[] = [];
  let afterId: number | null = null;
  for (let page = 0; page < maxPages; page += 1) {
    const result: FindingPage = await getJson<FindingPage>(
      "/api/v1/findings",
      { ...filters, after_id: afterId, limit: FINDINGS_PAGE },
      signal,
    );
    items.push(...result.items);
    afterId = result.next_after_id;
    if (afterId === null) return { items, truncated: false };
  }
  return { items, truncated: true };
}

/** A link to the Findings page with these filters, in the API's parameter names. */
export function findingsHref(filters: FindingFilters): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `/findings?${text}` : "/findings";
}
