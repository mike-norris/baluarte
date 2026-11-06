import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { TAGS, auditor, entries, manager, people, tag, timeSummary } from "../test/manager";

const PERIOD = "from=2026-10-07&to=2026-11-06";
const SUMMARY = `GET /api/v1/time/summary?${PERIOD}&by=group`;
const PEOPLE = `GET /api/v1/time/people?${PERIOD}&by=person`;
const ENTRIES = `GET /api/v1/time/people/dev.one?${PERIOD}`;

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, routes: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [TAGS]: () => json({ items: [tag(1, "team", "payments")] }),
    [SUMMARY]: () => json(timeSummary()),
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

describe("time summary", () => {
  it("sums time by group, against estimates, with a total", async () => {
    api(auditor);

    renderAt(`/manager/time?${PERIOD}`);

    const table = await screen.findByRole("table", { name: "Time by group" });
    const rows = within(table)
      .getAllByRole("row")
      .map((row) => row.textContent);
    expect(rows).toEqual([
      "GroupSpentEstimatedAgainst estimateIssuesEntries",
      "acme/payments12h 30m10h125% of estimate921",
      "acme/web30mNo estimateNo estimate12",
      "Total13h10h130% of estimate1023",
    ]);
    expect(screen.getByText("Time logs last read 6 Nov 2026, 06:00 UTC.")).toBeInTheDocument();
  });

  it("filters and regroups through the URL", async () => {
    const calls = api(auditor, {
      [`GET /api/v1/time/summary?${PERIOD}&tag=team%3Apayments&label=type%3A%3Abug&by=tag%3Ateam`]:
        () => json(timeSummary({ by: "tag:team", rows: [] })),
    });
    const user = userEvent.setup();
    const router = renderAt(`/manager/time?${PERIOD}`);

    const form = await screen.findByRole("form", { name: "Filter time" });
    await screen.findByRole("option", { name: "team=payments" });
    await user.selectOptions(within(form).getByLabelText("Tag"), "team:payments");
    await user.type(within(form).getByLabelText("Label"), "type::bug");
    await user.selectOptions(within(form).getByLabelText("Group by"), "tag:team");
    await user.click(within(form).getByRole("button", { name: "Apply" }));

    expect(await screen.findByText("No time was logged in this period.")).toBeInTheDocument();
    expect(router.state.location.search).toBe(
      `?${PERIOD}&tag=team%3Apayments&label=type%3A%3Abug&by=tag%3Ateam`,
    );
    expect(calls.at(-1)?.url).toContain("by=tag%3Ateam");
  });

  it("refuses a period that ends before it starts", async () => {
    api(auditor);
    const user = userEvent.setup();
    renderAt(`/manager/time?${PERIOD}`);

    const form = await screen.findByRole("form", { name: "Filter time" });
    const from = within(form).getByLabelText("From");
    await user.clear(from);
    await user.type(from, "2026-11-30");
    await user.click(within(form).getByRole("button", { name: "Apply" }));

    expect(within(form).getByRole("alert")).toHaveTextContent(
      "The start must be on or before the end.",
    );
  });
});

describe("time per person", () => {
  it("is never offered without the permission", async () => {
    api(auditor);
    renderAt(`/manager/time?${PERIOD}`);
    await screen.findByRole("table", { name: "Time by group" });
    expect(screen.queryByRole("heading", { name: "Time per person" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show time per person" })).not.toBeInTheDocument();
  });

  it("loads only when asked for, saying the view is audited", async () => {
    const calls = api(manager, { [PEOPLE]: () => json(people()) });
    const user = userEvent.setup();
    renderAt(`/manager/time?${PERIOD}`);

    const button = await screen.findByRole("button", {
      name: "Show time per person",
    });
    expect(screen.getByText(/Every view of it is recorded in Baluarte's audit log/)).toBeVisible();
    expect(calls.some((call) => call.url.startsWith("/api/v1/time/people"))).toBe(false);
    await user.click(button);

    const table = await screen.findByRole("table", { name: "Time per person" });
    expect(within(table).getByRole("link", { name: "dev.one" })).toHaveAttribute(
      "href",
      `/manager/time?${PERIOD}&person=dev.one`,
    );
    expect(table).toHaveTextContent("8h 30m");
  });

  it.each([
    ["out_of_scope", /shown only for the groups assigned to you/],
    ["per_person_disabled", /does not show time per person/],
  ])("explains a %s refusal", async (code, message) => {
    api(manager, {
      [PEOPLE]: () => json({ detail: { code, message: "refused" } }, 403),
    });
    const user = userEvent.setup();
    renderAt(`/manager/time?${PERIOD}`);

    await user.click(await screen.findByRole("button", { name: "Show time per person" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("is hidden when the deployment turns it off", async () => {
    api(manager, {
      [SUMMARY]: () => json(timeSummary({ per_person_enabled: false })),
    });
    renderAt(`/manager/time?${PERIOD}`);
    expect(
      await screen.findByText("This deployment does not show time per person."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show time per person" })).not.toBeInTheDocument();
  });

  it("lists one person's entries, with removals marked", async () => {
    api(manager, {
      [ENTRIES]: () => json(entries("gid://gitlab/Timelog/502")),
      [`${ENTRIES}&after=gid%3A%2F%2Fgitlab%2FTimelog%2F502`]: () =>
        json({
          ...entries(),
          items: [{ ...entries().items[0], id: "gid://gitlab/Timelog/503" }],
        }),
    });
    const user = userEvent.setup();
    renderAt(`/manager/time?${PERIOD}&person=dev.one`);

    const table = await screen.findByRole("table", {
      name: "Time entries of dev.one",
    });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("Issue #42");
    expect(rows[1]).toHaveTextContent("1h 30m");
    expect(rows[2]).toHaveTextContent("Merge request !7");
    expect(rows[2]).toHaveTextContent("Removed in GitLab, noticed 5 Nov 2026, 06:00 UTC");
    await user.click(screen.getByRole("button", { name: "Load more entries" }));
    expect(await screen.findAllByText("Issue #42")).toHaveLength(2);
  });

  it("shows the summary instead of entries to a user without the permission", async () => {
    api(auditor);
    renderAt(`/manager/time?${PERIOD}&person=dev.one`);
    expect(await screen.findByRole("table", { name: "Time by group" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Time logged by/ })).not.toBeInTheDocument();
  });
});
