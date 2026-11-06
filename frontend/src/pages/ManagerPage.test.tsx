import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import {
  EVALUATION_FRESHNESS,
  evaluationFreshness,
  freshness,
} from "../test/freshness";
import {
  TAGS,
  auditor,
  manager,
  overview,
  overviewRow,
  tag,
  trends,
} from "../test/manager";

const OVERVIEW = "GET /api/v1/manager/overview";
const TRENDS = "GET /api/v1/manager/trends?bucket=week";

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, routes: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [TAGS]: () => json({ items: [tag(1, "team", "payments")] }),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("manager navigation", () => {
  it("shows Manager only to users who may read it", async () => {
    api(analyst);
    renderAt("/inventory");
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(
      within(nav).queryByRole("link", { name: "Manager" }),
    ).not.toBeInTheDocument();
  });

  it.each([
    ["a manager", manager, ["Overview", "Trends", "Time", "Tags"]],
    ["an auditor", auditor, ["Overview", "Trends", "Time", "Tags"]],
  ])("gives %s the manager sections", async (_name, user, tabs) => {
    api(user, { [OVERVIEW]: () => json(overview([])) });
    renderAt("/manager");
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(
      within(nav).getByRole("link", { name: "Manager" }),
    ).toBeInTheDocument();
    const sections = await screen.findByRole("navigation", {
      name: "Manager space",
    });
    expect(
      within(sections)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(tabs);
  });
});

describe("manager overview", () => {
  it("rolls figures up by top-level group, each linked to its list", async () => {
    api(manager, {
      [OVERVIEW]: () =>
        json(
          overview([
            overviewRow("acme"),
            overviewRow("labs", {
              key: { type: "group", gitlab_id: 20, path: "labs" },
              label_drift: null,
              time_30d: null,
              controls_not_met: {},
              changes: {
                days_30: {
                  total: 0,
                  reviewed: 0,
                  direct_pushes: 0,
                  unknown: 0,
                  rate: null,
                },
                days_90: {
                  total: 0,
                  reviewed: 0,
                  direct_pushes: 0,
                  unknown: 0,
                  rate: null,
                },
              },
            }),
          ]),
        ),
    });

    renderAt("/manager");

    const acme = await screen.findByRole("article", { name: "acme" });
    expect(acme).toHaveTextContent("12 monitored, 2 excluded");
    expect(within(acme).getByRole("link", { name: "8 open" })).toHaveAttribute(
      "href",
      "/findings?state=open",
    );
    expect(acme).toHaveTextContent("1 critical, 3 high, 4 medium, 0 low");
    expect(
      within(acme).getByRole("link", { name: "SOC 2: 2; ISO 27001: 1" }),
    ).toHaveAttribute("href", "/controls");
    expect(acme).toHaveTextContent(
      "75% reviewed (30 of 40), 4 direct pushes, 1 unknown",
    );
    expect(within(acme).getByRole("link", { name: "5 items" })).toHaveAttribute(
      "href",
      "/labels?group=acme",
    );
    expect(
      within(acme).getByRole("link", { name: "10h spent, 15h estimated" }),
    ).toHaveAttribute("href", "/manager/time?group=acme");
    expect(within(acme).getByRole("link", { name: "acme" })).toHaveAttribute(
      "href",
      "/manager?group=acme",
    );

    const labs = screen.getByRole("article", { name: "labs" });
    expect(labs).toHaveTextContent("No label taxonomy in force");
    expect(labs).toHaveTextContent("Not collected yet");
    expect(labs).toHaveTextContent("No changes");
    expect(within(labs).getByText("None")).toBeInTheDocument();
  });

  it("drills into a group and back up", async () => {
    const calls = api(manager, {
      [OVERVIEW]: () => json(overview([overviewRow("acme")])),
      [`${OVERVIEW}?group=acme`]: () =>
        json(
          overview([
            overviewRow("acme/payments", {
              key: { type: "group", gitlab_id: 11, path: "acme/payments" },
            }),
          ]),
        ),
    });
    const user = userEvent.setup();
    const router = renderAt("/manager");

    await user.click(await screen.findByRole("link", { name: "acme" }));

    expect(
      await screen.findByRole("article", { name: "acme/payments" }),
    ).toBeInTheDocument();
    expect(router.state.location.search).toBe("?group=acme");
    expect(
      calls.some((call) => call.url === "/api/v1/manager/overview?group=acme"),
    ).toBe(true);
    await user.click(
      screen.getByRole("link", { name: "Back to all top-level groups" }),
    );
    expect(
      await screen.findByRole("article", { name: "acme" }),
    ).toBeInTheDocument();
  });

  it("rolls up by tag value, with findings filtered by the tag", async () => {
    api(manager, {
      [`${OVERVIEW}?tag=team%3Apayments`]: () =>
        json(
          overview(
            [
              overviewRow("acme", {
                key: { type: "tag", key: "team", value: "payments" },
              }),
            ],
            { parent: { type: "tag", key: "team", value: "payments" } },
          ),
        ),
    });

    renderAt("/manager?tag=team:payments");

    const row = await screen.findByRole("article", { name: "team=payments" });
    expect(within(row).getByRole("link", { name: "8 open" })).toHaveAttribute(
      "href",
      "/findings?state=open&tag=team%3Apayments",
    );
    expect(
      screen.getByText("Showing tag team=payments. Change"),
    ).toBeInTheDocument();
  });

  it("asks for the past and says which figures are current", async () => {
    const calls = api(manager, {
      [`${OVERVIEW}?as_of=2026-10-01T00%3A00%3A00Z`]: () =>
        json(
          overview([overviewRow("acme")], {
            as_of: "2026-10-01T00:00:00Z",
            evaluation_run: { id: 30, finished_at: "2026-09-30T22:00:00Z" },
          }),
        ),
    });

    renderAt("/manager?as_of=2026-10-01T00:00:00Z");

    expect(
      await screen.findByRole("article", { name: "acme" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/from evaluation run 30/)).toBeInTheDocument();
    expect(
      screen.getByText(
        "Shown as of now, because Baluarte keeps no history of them: time tracking, tags.",
      ),
    ).toBeInTheDocument();
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/manager/overview?as_of=2026-10-01T00%3A00%3A00Z",
    );
  });

  it("explains an empty roll-up", async () => {
    api(manager, { [OVERVIEW]: () => json(overview([])) });
    renderAt("/manager");
    expect(
      await screen.findByText("Nothing to show for all top-level groups."),
    ).toBeInTheDocument();
  });
});

describe("trends", () => {
  it("charts each measure with its data as a table", async () => {
    api(manager, { [TRENDS]: () => json(trends()) });

    renderAt("/manager/trends");

    const findings = await screen.findByRole("figure", {
      name: "Open findings",
    });
    expect(within(findings).getByRole("img")).toHaveAccessibleName(
      /Open findings from 19 Oct 2026 to 2 Nov 2026\. Latest: Critical 1, High 2, Medium 3, Low 0\./,
    );
    const passing = screen.getByRole("figure", { name: "Rules passing" });
    const table = within(passing).getByRole("table", { hidden: true });
    const rows = within(table).getAllByRole("row", { hidden: true });
    expect(rows.map((row) => row.textContent)).toEqual([
      "Period endingRules passing",
      "19 Oct 202680%",
      "26 Oct 2026No data",
      "2 Nov 202690%",
    ]);
    expect(
      screen.getByRole("figure", { name: "Time spent" }),
    ).toBeInTheDocument();
  });

  it("asks for monthly buckets over a chosen period", async () => {
    const calls = api(manager, {
      [TRENDS]: () => json(trends()),
      "GET /api/v1/manager/trends?bucket=month&from=2025-11-01&to=2026-10-31":
        () => json(trends({ bucket: "month", points: [] })),
    });
    const user = userEvent.setup();
    renderAt("/manager/trends");

    const form = await screen.findByRole("form", { name: "Trend period" });
    await user.selectOptions(within(form).getByLabelText("Period"), "month");
    await user.type(within(form).getByLabelText("From"), "2025-11-01");
    await user.type(within(form).getByLabelText("To"), "2026-10-31");
    await user.click(within(form).getByRole("button", { name: "Show" }));

    expect(
      await screen.findByText("No data in this period."),
    ).toBeInTheDocument();
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/manager/trends?bucket=month&from=2025-11-01&to=2026-10-31",
    );
  });

  it("explains a period that is too long", async () => {
    api(manager, {
      "GET /api/v1/manager/trends?bucket=week&from=2020-01-01&to=2026-01-01":
        () => json({ detail: "too many buckets" }, 422),
    });
    renderAt("/manager/trends?from=2020-01-01&to=2026-01-01");
    expect(
      await screen.findByText(
        "That period is too long: at most 105 weeks or 36 months.",
      ),
    ).toBeInTheDocument();
  });
});
