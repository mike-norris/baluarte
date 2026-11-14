import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { durationText, type IssuePlan, type IssuePlanStep } from "../api/issuePlans";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const PREVIEW = "GET /api/v1/issue-plans/preview";
const LIST = "GET /api/v1/issue-plans?limit=50";

const planner: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "remediation:plan", "remediation:approve"],
};
const approver: CurrentUser = { ...planner, user_id: 2, display_name: "Grace Hopper" };

function api(user: CurrentUser, extra: Record<string, (init?: RequestInit) => Response>): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...extra,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

const preview = {
  rules: { missing_time_label: "missing-time", estimate_round_minutes: 30, issue_types: ["issue"] },
  closed_issues: 12,
  last_read_at: "2026-11-13T10:00:00Z",
  add_label: 3,
  remove_label: 1,
  set_estimate: 4,
  commits_unknown: 2,
  writes_enabled: false,
  max_steps: 5000,
};

function step(overrides: Partial<IssuePlanStep> = {}): IssuePlanStep {
  return {
    step: 1,
    action: "set_estimate",
    project: { gitlab_id: 100, path: "acme/web" },
    issue: { iid: 7, type: "issue", title: "Fix login", web_url: "https://gitlab.example.test/7" },
    label: null,
    prior: { has_label: false, estimate_seconds: 0, spent_seconds: 600 },
    intended: { estimate_seconds: 1800 },
    status: "pending",
    request_status: null,
    verified: null,
    message: null,
    finished_at: null,
    ...overrides,
  };
}

function plan(overrides: Partial<IssuePlan> = {}): IssuePlan {
  return {
    id: 4,
    state: "draft",
    scope: { groups: [] },
    totals: { set_estimate: 1, add_label: 1 },
    steps: 2,
    manual: 1,
    plan_digest: "a".repeat(64),
    created_by: { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" },
    created_at: "2026-11-13T10:00:00Z",
    expires_at: "2026-11-14T10:00:00Z",
    decided_by: null,
    decided_at: null,
    finished_at: null,
    rules: preview.rules,
    step_items: [
      step(),
      step({
        step: 2,
        action: "add_label",
        label: "missing-time",
        issue: { iid: 8, type: "issue", title: null, web_url: null },
        intended: { has_label: true },
      }),
    ],
    manual_items: [
      {
        reason: "label_undefined",
        message: "The label is not defined for this project.",
        project: { gitlab_id: 101, path: "acme/api" },
        issues: [1, 2],
      },
    ],
    digest_valid: true,
    submitted_at: null,
    decision_note: null,
    review_required: true,
    self_approved: false,
    apply_started_at: null,
    failed_step: null,
    failure: null,
    progress: { pending: 2 },
    can_apply: { allowed: false, reasons: ["not_approved", "issue_writes_disabled"] },
    ...overrides,
  };
}

function bodyOf(calls: Call[], url: string): unknown {
  const call = calls.filter((c) => c.url === url && c.init?.method === "POST").at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

describe("issue plans", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("formats durations as GitLab does", () => {
    expect(durationText(1800)).toBe("30m");
    expect(durationText(5400)).toBe("1h 30m");
    expect(durationText(7200)).toBe("2h");
  });

  it("shows the rules and what a plan would change, then creates one", async () => {
    const calls = api(planner, {
      [PREVIEW]: () => json(preview),
      [LIST]: () => json({ items: [], next_after_id: null }),
      "POST /api/v1/issue-plans": () => json(plan(), 201),
      "GET /api/v1/issue-plans/4": () => json(plan()),
    });
    const user = userEvent.setup();
    const router = renderAt("/time/issue-plans");

    expect(await screen.findByText(/gets the label "missing-time"/)).toBeInTheDocument();
    expect(screen.getByText(/rounded up to 30m/)).toBeInTheDocument();
    expect(screen.getByText("Estimates to set").nextSibling).toHaveTextContent("4");
    expect(screen.getByText(/Issue writes are turned off/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Groups/), "acme");
    await user.click(screen.getByRole("button", { name: "Create a plan" }));

    expect(await screen.findByRole("heading", { name: "Issue plan 4" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/time/issue-plans/4");
    expect(bodyOf(calls, "/api/v1/issue-plans")).toEqual({ scope: { groups: ["acme"] } });
  });

  it("lists the steps, the issues to fix by hand and why it can't be applied", async () => {
    api(planner, { "GET /api/v1/issue-plans/4": () => json(plan({ state: "approved" })) });
    renderAt("/time/issue-plans/4");

    const table = await screen.findByRole("table", { name: /Plan steps/ });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("acme/web#7");
    expect(rows[1]).toHaveTextContent("Estimate 30m (spent 10m)");
    expect(rows[2]).toHaveTextContent("Add label missing-time");
    expect(screen.getByText("acme/api").closest("li")).toHaveTextContent("2 issues (#1, #2)");
    expect(screen.getByText(/BALUARTE_ISSUE_WRITES is not true/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply in GitLab" })).toBeDisabled();
  });

  it("lets someone else approve the reviewed digest", async () => {
    const calls = api(approver, {
      "GET /api/v1/issue-plans/4": () => json(plan({ state: "submitted" })),
      "POST /api/v1/issue-plans/4/approve": () => json(plan({ state: "approved" })),
    });
    const user = userEvent.setup();
    renderAt("/time/issue-plans/4");

    await user.click(await screen.findByRole("button", { name: "Approve this digest" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByText("Plan 4 approved.")).toBeInTheDocument();
    expect(bodyOf(calls, "/api/v1/issue-plans/4/approve")).toEqual({
      plan_digest: "a".repeat(64),
    });
  });

  it("leaves the creator's own plan to someone else outside development mode", async () => {
    api(planner, { "GET /api/v1/issue-plans/4": () => json(plan({ state: "submitted" })) });
    renderAt("/time/issue-plans/4");
    expect(await screen.findByText(/Someone else must approve or reject it/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve this digest" })).not.toBeInTheDocument();
  });

  it("lets the creator decide their own plan in development mode", async () => {
    const calls = api(
      { ...planner, development_mode: true },
      {
        "GET /api/v1/issue-plans/4": () => json(plan({ state: "submitted" })),
        "POST /api/v1/issue-plans/4/approve": () => json(plan({ state: "approved" })),
      },
    );
    const user = userEvent.setup();
    renderAt("/time/issue-plans/4");

    await user.click(await screen.findByRole("button", { name: "Approve this digest" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(await screen.findByText("Plan 4 approved.")).toBeInTheDocument();
    expect(bodyOf(calls, "/api/v1/issue-plans/4/approve")).toEqual({
      plan_digest: "a".repeat(64),
    });
  });

  it("offers approve and reject on a phone, but never apply", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width: 767.98px"),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    api(approver, { "GET /api/v1/issue-plans/4": () => json(plan({ state: "submitted" })) });
    renderAt("/time/issue-plans/4");

    const decision = await screen.findByRole("region", { name: "Decision" });
    expect(within(decision).getByRole("button", { name: "Approve this digest" })).toBeEnabled();
    expect(within(decision).getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(within(decision).queryByRole("button", { name: "Apply in GitLab" })).not.toBeInTheDocument();
    expect(decision).toHaveTextContent("Apply: Available on a larger screen.");
  });

  it("shows issues that changed since the plan as left alone", async () => {
    api(planner, {
      "GET /api/v1/issue-plans/4": () =>
        json(
          plan({
            state: "applied",
            finished_at: "2026-11-13T11:00:00Z",
            step_items: [step({ status: "stale", message: "The issue has an estimate now." })],
            progress: { stale: 1 },
          }),
        ),
    });
    renderAt("/time/issue-plans/4");
    expect(await screen.findByText("Changed since the plan; left alone")).toBeInTheDocument();
    expect(screen.getByText(/1 changed since the plan/)).toBeInTheDocument();
  });
});
