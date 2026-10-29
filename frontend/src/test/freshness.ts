import type { EvaluationFreshness, EvaluationRun } from "../api/evaluations";
import type { FreshnessReport, KindFreshness } from "../api/gitlab";

export const EVALUATION_FRESHNESS = "GET /api/v1/evaluations/freshness";

export function evaluationRun(id: number, extra: Partial<EvaluationRun> = {}): EvaluationRun {
  return {
    id,
    status: "succeeded",
    started_at: "2026-10-29T06:10:00Z",
    finished_at: "2026-10-29T06:12:00Z",
    projects: 3,
    counts: {},
    error: null,
    ...extra,
  };
}

export function evaluationFreshness(
  overrides: Partial<EvaluationFreshness> = {},
): EvaluationFreshness {
  return {
    status: "fresh",
    checked_at: "2026-10-29T07:00:00Z",
    stale_after_minutes: 720,
    running: false,
    queued: false,
    behind_collection: false,
    last_run: evaluationRun(7),
    last_success_at: "2026-10-29T06:12:00Z",
    ...overrides,
  };
}

export function kind(
  status: KindFreshness["status"],
  extra: Partial<KindFreshness> = {},
): KindFreshness {
  return {
    status,
    running: false,
    last_run: null,
    last_success_at: status === "never" ? null : "2026-10-29T06:00:00Z",
    ...extra,
  };
}

export function freshness(overrides: Partial<FreshnessReport> = {}): FreshnessReport {
  return {
    status: "fresh",
    checked_at: "2026-10-29T07:00:00Z",
    stale_after_minutes: 720,
    sync_interval_minutes: 360,
    collections: {
      capabilities: kind("fresh"),
      groups: kind("fresh"),
      projects: kind("fresh"),
    },
    resources: {},
    ...overrides,
  };
}
