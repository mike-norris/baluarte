import type { AppUser, ManagerScope, Overview, OverviewRow, Trends } from "../api/manager";
import type { Tag, TagDetail } from "../api/tags";
import type { PeopleSummary, PersonEntries, TimeSettings, TimeSummary } from "../api/time";
import type { CurrentUser } from "../auth/session";
import { analyst } from "./api";

export const TAGS = "GET /api/v1/tags";
export const SETTINGS_TIME = "GET /api/v1/settings/time";
export const SCOPES_ACTIVE = "GET /api/v1/manager/scopes?state=active";
export const USERS = "GET /api/v1/manager/users";

const person = { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" };

/** A manager: reads, the manager space, time including per person, and tags. */
export const manager: CurrentUser = {
  ...analyst,
  user_id: 3,
  subject: "user-3",
  email: "grace@example.test",
  display_name: "Grace Hopper",
  roles: ["manager"],
  permissions: [
    "evidence:read",
    "findings:read",
    "inventory:read",
    "manager:read",
    "time:read",
    "time:read_people",
    "tags:write",
  ],
};

/** An auditor in the manager space: totals, never time per person, no tagging. */
export const auditor: CurrentUser = {
  ...analyst,
  user_id: 4,
  subject: "user-4",
  display_name: "Alan Turing",
  roles: ["auditor"],
  permissions: ["evidence:read", "findings:read", "inventory:read", "manager:read", "time:read"],
};

export function overviewRow(path: string, extra: Partial<OverviewRow> = {}): OverviewRow {
  return {
    key: { type: "group", gitlab_id: 10, path },
    projects: { monitored: 12, excluded: 2 },
    findings_open: { critical: 1, high: 3, medium: 4, low: 0, total: 8 },
    controls_not_met: { soc2: 2, iso27001: 1 },
    changes: {
      days_30: { total: 40, reviewed: 30, direct_pushes: 4, unknown: 1, rate: 0.75 },
      days_90: { total: 120, reviewed: 100, direct_pushes: 6, unknown: 0, rate: 100 / 120 },
    },
    label_drift: 5,
    time_30d: { spent_seconds: 36_000, estimate_seconds: 54_000 },
    ...extra,
  };
}

export function overview(rows: OverviewRow[], extra: Partial<Overview> = {}): Overview {
  return {
    as_of: null,
    evaluation_run: { id: 41, finished_at: "2026-11-06T09:00:00Z" },
    current_only: ["time", "tags"],
    parent: { type: "instance" },
    rows,
    ...extra,
  };
}

export function trends(extra: Partial<Trends> = {}): Trends {
  const point = (end: string, total: number, rate: number | null) => ({
    end,
    evaluation_run_id: 40,
    findings_open: { critical: 1, high: 2, medium: total - 3, low: 0, total },
    rule_pass_rate: rate,
    change_review_rate: rate === null ? null : rate - 0.1,
    time_spent_seconds: 7_200,
  });
  return {
    bucket: "week",
    from: "2026-10-19",
    to: "2026-11-09",
    projects: 14,
    current_only: [],
    points: [
      point("2026-10-19T00:00:00Z", 10, 0.8),
      point("2026-10-26T00:00:00Z", 8, null),
      point("2026-11-02T00:00:00Z", 6, 0.9),
    ],
    ...extra,
  };
}

export function tag(id: number, key: string, value: string, extra: Partial<Tag> = {}): Tag {
  return {
    id,
    key,
    value,
    description: null,
    color: "#1f6feb",
    created_by: person,
    created_at: "2026-11-05T10:00:00Z",
    groups: 1,
    projects: 6,
    ...extra,
  };
}

export function tagDetail(base: Tag, extra: Partial<TagDetail> = {}): TagDetail {
  return {
    ...base,
    assignments: [
      {
        id: 7,
        target_type: "group",
        target_gitlab_id: 110,
        target_path: "acme/payments",
        assigned_by: person,
        assigned_at: "2026-11-05T10:05:00Z",
      },
    ],
    ...extra,
  };
}

export function timeSummary(extra: Partial<TimeSummary> = {}): TimeSummary {
  return {
    period: { from: "2026-10-07", to: "2026-11-07" },
    by: "group",
    rows: [
      {
        key: "acme/payments",
        label: "acme/payments",
        spent_seconds: 45_000,
        estimate_seconds: 36_000,
        issues: 9,
        entries: 21,
      },
      {
        key: "acme/web",
        label: "acme/web",
        spent_seconds: 1_800,
        estimate_seconds: null,
        issues: 1,
        entries: 2,
      },
    ],
    totals: { spent_seconds: 46_800, estimate_seconds: 36_000, issues: 10, entries: 23 },
    per_person_enabled: true,
    collected_at: "2026-11-06T06:00:00Z",
    ...extra,
  };
}

export function people(): PeopleSummary {
  return {
    period: { from: "2026-10-07", to: "2026-11-07" },
    rows: [{ username: "dev.one", spent_seconds: 30_600, issues: 6, entries: 14 }],
    totals: { spent_seconds: 30_600, estimate_seconds: null, issues: 6, entries: 14 },
    all_projects: false,
    collected_at: "2026-11-06T06:00:00Z",
  };
}

export function entries(next: string | null = null): PersonEntries {
  return {
    username: "dev.one",
    period: { from: "2026-10-07", to: "2026-11-07" },
    items: [
      {
        id: "gid://gitlab/Timelog/501",
        project: { gitlab_id: 120, path: "acme/payments/api" },
        spent_at: "2026-11-04T15:00:00Z",
        seconds: 5_400,
        issue_iid: 42,
        merge_request_iid: null,
        labels: ["type::bug"],
        milestone: "2026.11",
        removed_at: null,
      },
      {
        id: "gid://gitlab/Timelog/502",
        project: { gitlab_id: 120, path: "acme/payments/api" },
        spent_at: "2026-11-03T09:00:00Z",
        seconds: 1_800,
        issue_iid: null,
        merge_request_iid: 7,
        labels: [],
        milestone: null,
        removed_at: "2026-11-05T06:00:00Z",
      },
    ],
    next_after: next,
  };
}

export function timeSettings(extra: Partial<TimeSettings> = {}): TimeSettings {
  return {
    lookback_days: 180,
    reread_days: 30,
    retention_days: null,
    per_person_enabled: true,
    ...extra,
  };
}

export function scope(id: number, path: string): ManagerScope {
  return {
    id,
    user: { actor: "user:3", user_id: 3, display_name: "Grace Hopper" },
    group: { gitlab_id: 110, path },
    assigned_by: person,
    assigned_at: "2026-11-05T11:00:00Z",
    ended_by: null,
    ended_at: null,
  };
}

export const users: AppUser[] = [
  {
    id: 1,
    display_name: "Ada Lovelace",
    email: "ada@example.test",
    roles: ["admin"],
    last_login_at: "2026-11-06T08:00:00Z",
  },
  {
    id: 3,
    display_name: "Grace Hopper",
    email: "grace@example.test",
    roles: ["manager"],
    last_login_at: "2026-11-05T08:00:00Z",
  },
];
