import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { findingsHref, type Finding } from "../api/evaluations";
import { readFindingFilters, writeFindingFilters } from "../findings/filters";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { finding } from "../test/findings";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const ALL = "GET /api/v1/findings?limit=50";

function page(items: Finding[], next: number | null = null) {
  return json({ items, next_after_id: next });
}

function api(routes: Record<string, () => Response>) {
  return mockApi({
    [ME]: () => json(analyst),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("findings list", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists findings with policy version, severity, state and latest outcome", async () => {
    api({
      [ALL]: () =>
        page([
          finding(1),
          finding(2, { latest_outcome: "unknown", severity: "medium" }),
          finding(3, {
            state: "resolved",
            latest_outcome: "pass",
            resolution: "passed",
            resolved_at: "2026-10-29T06:12:00Z",
          }),
        ]),
    });
    renderAt("/findings");

    const table = await screen.findByRole("table", { name: "Findings" });
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    const row = (path: string) =>
      within(table).getByRole("rowheader", { name: path }).closest("tr");
    expect(row("platform/p1")).toHaveTextContent("corporate-default v2");
    expect(row("platform/p1")).toHaveTextContent("High");
    expect(row("platform/p1")).toHaveTextContent("Open");
    expect(row("platform/p1")).toHaveTextContent("Fail");
    expect(row("platform/p1")).toHaveTextContent("28 Oct 2026, 06:00 UTC");
    expect(row("platform/p2")).toHaveTextContent("Unknown");
    expect(row("platform/p2")).toHaveTextContent("Not counted as passing.");
    expect(row("platform/p3")).toHaveTextContent("Resolved");
    expect(within(table).getByRole("link", { name: "platform/p1" })).toHaveAttribute(
      "href",
      "/findings/1",
    );
    expect(screen.getByText("Showing 3 findings.")).toBeInTheDocument();
    expect(screen.getByText("Policy evaluation: Up to date")).toBeInTheDocument();
  });

  it("opens the dashboard's links with the same filters sent to the API", async () => {
    const href = findingsHref({ state: "open", outcome: "unknown" });
    const calls = api({
      "GET /api/v1/findings?state=open&outcome=unknown&limit=50": () =>
        page([finding(4, { latest_outcome: "unknown" })]),
    });
    renderAt(href);

    expect(await screen.findByRole("rowheader", { name: "platform/p4" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "State" })).toHaveValue("open");
    expect(screen.getByRole("combobox", { name: "Latest outcome" })).toHaveValue("unknown");
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/findings?state=open&outcome=unknown&limit=50",
    );
  });

  it.each([
    "/findings?state=open&severity=critical",
    "/findings?state=open&policy=corporate-default&rule=project-visibility",
    "/findings?project=105",
  ])("round-trips %s through the URL", (path) => {
    const query = new URLSearchParams(path.split("?")[1]);
    expect(writeFindingFilters(readFindingFilters(query)).toString()).toBe(query.toString());
  });

  it("drops filter values the API would reject", () => {
    const filters = readFindingFilters(
      new URLSearchParams({
        state: "closed",
        outcome: "maybe",
        severity: "urgent",
        project: "-3",
        rule: "r".repeat(64),
      }),
    );
    expect(filters).toEqual({});
  });

  it("applies and clears filters", async () => {
    const user = userEvent.setup();
    const calls = api({
      [ALL]: () => page([finding(1)]),
      "GET /api/v1/findings?severity=critical&project=105&rule=protected-default-branch&limit=50":
        () => page([]),
    });
    const router = renderAt("/findings");
    await screen.findByRole("rowheader", { name: "platform/p1" });

    await user.selectOptions(screen.getByRole("combobox", { name: "Severity" }), "critical");
    await user.type(screen.getByRole("textbox", { name: "Rule ID" }), "protected-default-branch");
    await user.type(screen.getByRole("spinbutton", { name: "Project GitLab ID" }), "105");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("No findings match these filters.")).toBeInTheDocument();
    expect(router.state.location.search).toBe(
      "?severity=critical&project=105&rule=protected-default-branch",
    );
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/findings?severity=critical&project=105&rule=protected-default-branch&limit=50",
    );

    await user.click(screen.getByRole("link", { name: "Clear" }));
    expect(router.state.location.search).toBe("");
    expect(await screen.findByRole("rowheader", { name: "platform/p1" })).toBeInTheDocument();
  });

  it("loads more pages on request", async () => {
    const user = userEvent.setup();
    api({
      [ALL]: () => page([finding(1), finding(2)], 2),
      "GET /api/v1/findings?after_id=2&limit=50": () => page([finding(3)]),
    });
    renderAt("/findings");

    expect(await screen.findByText("Showing 2 findings (more available).")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more findings" }));

    expect(await screen.findByRole("rowheader", { name: "platform/p3" })).toBeInTheDocument();
    expect(screen.getByText("Showing 3 findings.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more findings" })).not.toBeInTheDocument();
  });

  it("does not present an empty list as compliance", async () => {
    api({ [ALL]: () => page([]) });
    renderAt("/findings");

    expect(
      await screen.findByText(/No findings yet\. .*it does not mean every rule passed\./),
    ).toBeInTheDocument();
  });

  it("offers a retry when the API fails", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    api({
      [ALL]: () => {
        attempts += 1;
        return attempts === 1 ? json({ detail: "boom" }, 500) : page([finding(1)]);
      },
    });
    renderAt("/findings");

    expect(await screen.findByText("The API returned an error (500).")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("rowheader", { name: "platform/p1" })).toBeInTheDocument();
  });

  it("warns when evaluation is stale", async () => {
    api({
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness({ status: "stale" })),
      [ALL]: () => page([finding(1)]),
    });
    renderAt("/findings");

    expect(
      await screen.findByText(
        "These results are not current compliance until an evaluation succeeds again.",
      ),
    ).toBeInTheDocument();
  });
});
