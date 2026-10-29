import type { FreshnessReport, KindFreshness } from "../api/gitlab";

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
    collections: { capabilities: kind("fresh"), groups: kind("fresh"), projects: kind("fresh") },
    resources: {},
    ...overrides,
  };
}
