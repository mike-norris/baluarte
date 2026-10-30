import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EvaluationRun, Finding } from "../api/evaluations";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import {
  EVALUATION_FRESHNESS,
  evaluationFreshness,
  evaluationRun,
  freshness,
} from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const RUNS = "GET /api/v1/evaluations/runs?limit=100";
const OPEN = "GET /api/v1/findings?state=open&limit=200";

function finding(id: number, extra: Partial<Finding> = {}): Finding {
  return {
    id,
    project: {
      gitlab_id: 100 + id,
      path: `platform/p${String(id)}`,
      state: "active",
    },
    policy: { policy_id: 1, name: "corporate-default", version: 2 },
    rule_id: "project-visibility",
    severity: "medium",
    state: "open",
    resolution: null,
    latest_outcome: "fail",
    first_seen_at: "2026-10-28T06:00:00Z",
    last_seen_at: "2026-10-29T06:12:00Z",
    resolved_at: null,
    exception: null,
    ...extra,
  };
}

function runs(items: EvaluationRun[]) {
  return json({ items, next_before_id: null });
}

function findings(items: Finding[], next: number | null = null) {
  return json({ items, next_after_id: next });
}

function api(routes: Record<string, () => Response | Promise<Response>>) {
  return mockApi({
    [ME]: () => json(analyst),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [RUNS]: () => runs([]),
    [OPEN]: () => findings([]),
    ...routes,
  });
}

function renderDashboard() {
  const router = createMemoryRouter(routes, { initialEntries: ["/"] });
  render(<RouterProvider router={router} />);
  return router;
}

const counted = evaluationRun(7, {
  projects: 4,
  counts: { outcomes: { pass: 3, fail: 2, unknown: 4, unsupported: 1 } },
});

describe("dashboard", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("totals every outcome and never counts unknown or unsupported as passing", async () => {
    api({ [RUNS]: () => runs([counted]) });
    renderDashboard();

    const table = await screen.findByRole("table", {
      name: "Rule results by outcome",
    });
    const row = (name: string) => within(table).getByRole("rowheader", { name }).closest("tr");
    expect(row("Pass")).toHaveTextContent("3");
    expect(row("Fail")).toHaveTextContent("2");
    expect(row("Unknown")).toHaveTextContent("4");
    expect(row("Unknown")).toHaveTextContent("Not counted as passing.");
    expect(row("Unsupported")).toHaveTextContent("1");
    expect(row("Excepted")).toHaveTextContent("0");
    // 3 of 10, not 3 of 5: unknown and unsupported stay in the denominator.
    expect(screen.getByText(/3 of 10 rule results passed \(30%\)/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /From evaluation run 7, finished 29 Oct 2026, 06:12 UTC, covering 4 projects/,
      ),
    ).toBeInTheDocument();
  });

  it("never counts excepted results as passing", async () => {
    api({
      [RUNS]: () => runs([evaluationRun(7, { counts: { outcomes: { pass: 1, excepted: 3 } } })]),
    });
    renderDashboard();

    const table = await screen.findByRole("table", { name: "Rule results by outcome" });
    const excepted = within(table).getByRole("rowheader", { name: "Excepted" }).closest("tr");
    expect(excepted).toHaveTextContent("3");
    expect(excepted).toHaveTextContent("Not counted as passing.");
    expect(screen.getByText(/1 of 4 rule results passed \(25%\)/)).toBeInTheDocument();
  });

  it("floors the pass share so it never rounds up to 100%", async () => {
    api({
      [RUNS]: () =>
        runs([
          evaluationRun(7, {
            counts: { outcomes: { pass: 1999, unknown: 1 } },
          }),
        ]),
    });
    renderDashboard();

    expect(
      await screen.findByText(/1999 of 2000 rule results passed \(99\.9%\)/),
    ).toBeInTheDocument();
  });

  it("uses the latest successful run and says when a newer one did not succeed", async () => {
    api({
      [RUNS]: () =>
        runs([
          evaluationRun(8, {
            status: "failed",
            finished_at: "2026-10-29T06:40:00Z",
            error: "boom",
          }),
          counted,
        ]),
    });
    renderDashboard();

    expect(await screen.findByText(/From evaluation run 7/)).toBeInTheDocument();
    expect(
      screen.getByText(
        "A newer run (8) has not succeeded, so these totals come from an earlier run.",
      ),
    ).toBeInTheDocument();
  });

  it("shows no totals before any evaluation has succeeded", async () => {
    api({
      [EVALUATION_FRESHNESS]: () =>
        json(
          evaluationFreshness({
            status: "never",
            last_run: null,
            last_success_at: null,
          }),
        ),
    });
    renderDashboard();

    expect(
      await screen.findByText(/No evaluation has completed successfully yet/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("table", { name: "Rule results by outcome" }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByRole("heading", {
        name: "Evaluation status: Never evaluated",
      }),
    ).toBeInTheDocument();
  });

  it("groups open findings by severity and rule, with links the Findings page can read", async () => {
    api({
      [OPEN]: () =>
        findings([
          finding(1, { severity: "high" }),
          finding(2, { severity: "high", rule_id: "protected-default-branch" }),
          finding(3, { latest_outcome: "unknown" }),
        ]),
    });
    renderDashboard();

    expect(await screen.findByText("3 open findings.")).toBeInTheDocument();
    const bySeverity = screen.getByRole("table", {
      name: "Open findings by severity",
    });
    expect(within(bySeverity).getByRole("link", { name: "2 open high findings" })).toHaveAttribute(
      "href",
      "/findings?state=open&severity=high",
    );
    expect(
      within(bySeverity).getByRole("rowheader", { name: "Critical" }).closest("tr"),
    ).toHaveTextContent("0");

    const byRule = screen.getByRole("table", { name: "Open findings by rule" });
    expect(
      within(byRule).getByRole("link", {
        name: "2 open findings for corporate-default project-visibility",
      }),
    ).toHaveAttribute(
      "href",
      "/findings?state=open&policy=corporate-default&rule=project-visibility",
    );

    expect(screen.getByText(/1 of them could not be re-checked/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", {
        name: "Open findings with an unknown latest result",
      }),
    ).toHaveAttribute("href", "/findings?state=open&outcome=unknown");
  });

  it("says 'at least' when the findings were truncated", async () => {
    let page = 0;
    const calls = api({
      [OPEN]: () => findings([finding(1)], 1),
      ...Object.fromEntries(
        [1, 2, 3, 4].map((after) => [
          `GET /api/v1/findings?state=open&after_id=${String(after)}&limit=200`,
          () => {
            page += 1;
            return findings([finding(after + 1)], after + 1);
          },
        ]),
      ),
    });
    renderDashboard();

    expect(await screen.findByText(/At least 5 open findings\./)).toBeInTheDocument();
    expect(screen.getByText(/Only the first 1000 were counted/)).toBeInTheDocument();
    expect(page).toBe(4);
    expect(calls.filter((call) => call.url.startsWith("/api/v1/findings")).length).toBe(5);
    const bySeverity = screen.getByRole("table", {
      name: "Open findings by severity",
    });
    expect(
      within(bySeverity).getByRole("link", {
        name: "At least 5 open medium findings",
      }),
    ).toBeInTheDocument();
  });

  it("does not read no open findings as compliance", async () => {
    api({});
    renderDashboard();

    expect(
      await screen.findByText(/No open findings\. This does not mean every project complies/),
    ).toBeInTheDocument();
  });

  it("alerts on a failed evaluation and shows its error", async () => {
    api({
      [EVALUATION_FRESHNESS]: () =>
        json(
          evaluationFreshness({
            status: "failed",
            last_run: evaluationRun(8, {
              status: "failed",
              error: "evaluation failed: database unavailable",
            }),
          }),
        ),
    });
    renderDashboard();

    expect(
      await screen.findByRole("heading", { name: "Evaluation status: Failed" }),
    ).toBeInTheDocument();
    const section = screen.getByRole("region", {
      name: "Evaluation status: Failed",
    });
    expect(within(section).getByRole("alert")).toHaveTextContent(
      "The latest policy evaluation failed. Results below may be out of date.",
    );
    expect(section).toHaveTextContent("Last run failed: evaluation failed: database unavailable");
  });

  it("notes results that predate the latest collection", async () => {
    api({
      [EVALUATION_FRESHNESS]: () =>
        json(evaluationFreshness({ behind_collection: true, queued: true })),
    });
    renderDashboard();

    expect(
      await screen.findByText(/may not reflect it yet \(an evaluation is on its way\)\./),
    ).toBeInTheDocument();
    expect(screen.getByText("An evaluation is queued.")).toBeInTheDocument();
  });

  it("offers a retry when results fail to load", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    api({
      [RUNS]: () => {
        attempts += 1;
        return attempts === 1 ? json({ detail: "boom" }, 500) : runs([counted]);
      },
    });
    renderDashboard();

    expect(
      await screen.findByText("Could not load rule results: The API returned an error (500)."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(
      await screen.findByRole("table", { name: "Rule results by outcome" }),
    ).toBeInTheDocument();
  });

  it("returns to sign-in when the session has ended", async () => {
    let signedIn = true;
    mockApi({
      [ME]: () => (signedIn ? json(analyst) : json({ detail: "no session" }, 401)),
      [FRESHNESS]: () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
      [RUNS]: () => {
        signedIn = false;
        return json({ detail: "no session" }, 401);
      },
      [OPEN]: () => json({ detail: "no session" }, 401),
    });
    renderDashboard();

    expect(await screen.findByRole("heading", { name: "Sign in to Baluarte" })).toBeInTheDocument();
  });
});
