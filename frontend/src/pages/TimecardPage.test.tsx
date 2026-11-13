import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Timecard, TimecardRow } from "../api/time";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { auditor, manager } from "../test/manager";
import { nextSort, sortRows } from "./TimecardPage";

const PERIOD = "from=2026-10-07&to=2026-11-06";
const CARD = `GET /api/v1/time/timecard?${PERIOD}&sort=date%2Cdeveloper`;

function row(id: number, extra: Partial<TimecardRow> = {}): TimecardRow {
  return {
    entry_id: id,
    timelog_id: `gid://gitlab/Timelog/${String(id)}`,
    kind: "logged",
    developer: "Dana One",
    username: "dev1",
    spent_on: "2026-11-02",
    spent_at: "2026-11-02T09:00:00Z",
    minutes: 60,
    removed_on: null,
    project: { gitlab_id: 100, path: "acme/team/web" },
    work_item: "#7",
    title: "Login form",
    url: "https://gitlab.example.test/acme/team/web/-/issues/7",
    milestone: "M2",
    iteration: "Sprints: 2026-11-02 to 2026-11-15",
    parent: { title: "Sign-in", reference: "acme&3" },
    top: { title: "Customer portal", reference: "acme&1" },
    estimate_minutes: 120,
    context_read: true,
    ...extra,
  };
}

function card(): Timecard {
  return {
    period: { from: "2026-10-07", to: "2026-11-07" },
    sort: ["date", "developer"],
    rows: [
      row(1),
      row(2, {
        developer: "Dev Two",
        username: "dev2",
        spent_on: "2026-11-03",
        spent_at: "2026-11-03T09:00:00Z",
        minutes: 30,
        work_item: "#8",
        title: "Password reset",
        parent: { title: "Login form", reference: "acme/team/web#7" },
        estimate_minutes: null,
      }),
      row(2, {
        kind: "removed",
        developer: "Dev Two",
        username: "dev2",
        spent_on: "2026-11-03",
        spent_at: "2026-11-03T09:00:00Z",
        minutes: -30,
        removed_on: "2026-11-05",
        work_item: "#8",
        title: "Password reset",
        parent: { title: "Login form", reference: "acme/team/web#7" },
        estimate_minutes: null,
      }),
      row(3, {
        spent_on: "2026-10-28",
        spent_at: "2026-10-28T09:00:00Z",
        minutes: 10,
        work_item: "!4",
        title: null,
        url: null,
        milestone: null,
        iteration: null,
        parent: null,
        top: null,
        estimate_minutes: null,
        context_read: false,
      }),
    ],
    totals: { minutes: 70, logged_minutes: 100, removed_minutes: -30, rows: 4, people: 2 },
    per_person: { "Dana One": 70, "Dev Two": 0 },
    all_projects: false,
    collected_at: "2026-11-06T06:00:00Z",
  };
}

function api(user: CurrentUser, routes: Record<string, () => Response> = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [CARD]: () => json(card()),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function bodyRows(table: HTMLElement): string[] {
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((tr) => within(tr).getAllByRole("cell")[0]?.textContent ?? "");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("timecard", () => {
  it("shows each log with its context, balancing removed time with a negative row", async () => {
    api(manager);
    renderAt(`/manager/timecard?${PERIOD}`);

    const table = await screen.findByRole("table", { name: "Timecard rows, sortable by column" });
    const rows = within(table).getAllByRole("row");
    expect(rows[0]).toHaveTextContent(
      "Date▲DeveloperMinutesProject (top-level epic)ParentIssueMilestoneIterationEstimate (minutes)RepositoryEntry",
    );
    expect(rows[1]).toHaveTextContent("2026-10-28Dana One10!4acme/team/webLogged");
    expect(rows[2]).toHaveTextContent(
      "2026-11-02Dana One60Customer portalSign-in#7Login formM2Sprints: 2026-11-02 to 2026-11-15120acme/team/webLogged",
    );
    expect(rows[4]).toHaveTextContent("-30");
    expect(rows[4]).toHaveTextContent("Removed in GitLab (noticed 2026-11-05)");
    expect(within(table).getByRole("link", { name: "#7" })).toHaveAttribute(
      "href",
      "https://gitlab.example.test/acme/team/web/-/issues/7",
    );
    expect(screen.getByText("70 min (1h 10m)")).toBeInTheDocument();
    expect(screen.getByText("-30 min")).toBeInTheDocument();
    expect(screen.getByText(/Every view and download is recorded/)).toBeVisible();
  });

  it("offers CSV and HTML downloads of the same rows", async () => {
    api(manager);
    renderAt(`/manager/timecard?${PERIOD}&person=dev2&sort=-minutes`);
    const csv = await screen.findByRole("link", { name: "Download CSV" });
    expect(csv).toHaveAttribute(
      "href",
      `/api/v1/time/timecard?${PERIOD}&person=dev2&sort=-minutes%2Cdate%2Cdeveloper&format=csv`.replace(
        "%2Cdate%2Cdeveloper",
        "",
      ),
    );
    expect(csv).toHaveAttribute("download");
    expect(screen.getByRole("link", { name: "Download HTML" }).getAttribute("href")).toContain(
      "format=html",
    );
  });

  it("sorts by a column in the page without reading the timecard again", async () => {
    const calls = api(manager);
    const user = userEvent.setup();
    const router = renderAt(`/manager/timecard?${PERIOD}`);
    const table = await screen.findByRole("table", { name: "Timecard rows, sortable by column" });
    const reads = calls.filter((c) => c.url.startsWith("/api/v1/time/timecard")).length;

    await user.click(within(table).getByRole("button", { name: "Minutes" }));
    expect(router.state.location.search).toBe(`?${PERIOD}&sort=minutes%2Cdate%2Cdeveloper`);
    expect(bodyRows(table)).toEqual(["2026-11-03", "2026-10-28", "2026-11-03", "2026-11-02"]);
    expect(within(table).getByRole("columnheader", { name: /Minutes/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );

    await user.click(within(table).getByRole("button", { name: /Minutes/ }));
    expect(router.state.location.search).toBe(`?${PERIOD}&sort=-minutes%2Cdate%2Cdeveloper`);
    expect(bodyRows(table)[0]).toBe("2026-11-02");
    expect(calls.filter((c) => c.url.startsWith("/api/v1/time/timecard")).length).toBe(reads);
  });

  it("filters through the API with the chosen developer and epic", async () => {
    const filtered = `GET /api/v1/time/timecard?${PERIOD}&person=dev2&top=Customer+portal&removed=false&sort=date%2Cdeveloper`;
    const calls = api(manager, { [filtered]: () => json({ ...card(), rows: [] }) });
    const user = userEvent.setup();
    const router = renderAt(`/manager/timecard?${PERIOD}`);

    const form = await screen.findByRole("form", { name: "Timecard filters" });
    await screen.findByRole("option", { name: "Dev Two (dev2)" });
    await user.selectOptions(within(form).getByLabelText("Developer"), "dev2");
    await user.selectOptions(
      within(form).getByLabelText("Project (top-level epic)"),
      "Customer portal",
    );
    expect(
      within(within(form).getByLabelText("Project (top-level epic)")).getByRole("option", {
        name: "(none)",
      }),
    ).toBeInTheDocument();
    await user.click(within(form).getByLabelText("Include time removed in GitLab"));
    await user.click(within(form).getByRole("button", { name: "Show timecard" }));

    expect(await screen.findByText("No time logged with these filters.")).toBeInTheDocument();
    expect(router.state.location.search).toBe(
      `?${PERIOD}&person=dev2&top=Customer+portal&removed=false`,
    );
    expect(calls.at(-1)?.url).toBe(filtered.replace("GET ", ""));
  });

  it("refuses a period that ends before it starts", async () => {
    api(manager);
    const user = userEvent.setup();
    renderAt(`/manager/timecard?${PERIOD}`);
    const form = await screen.findByRole("form", { name: "Timecard filters" });
    const from = within(form).getByLabelText("From");
    await user.clear(from);
    await user.type(from, "2026-11-30");
    await user.click(within(form).getByRole("button", { name: "Show timecard" }));
    expect(within(form).getByRole("alert")).toHaveTextContent(
      "The start must be on or before the end.",
    );
  });

  it("is not offered without time per person", async () => {
    const calls = api(auditor);
    renderAt(`/manager/timecard?${PERIOD}`);
    expect(
      await screen.findByText("You do not have permission to see time per person."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Timecard" })).not.toBeInTheDocument();
    expect(calls.some((c) => c.url.startsWith("/api/v1/time/timecard"))).toBe(false);
  });

  it("explains why the API refused it", async () => {
    api(manager, {
      [CARD]: () => json({ detail: { code: "out_of_scope", message: "refused" } }, 403),
    });
    renderAt(`/manager/timecard?${PERIOD}`);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /shown only for the groups assigned to you/,
    );
  });
});

describe("timecard sorting", () => {
  it("puts empty values last and a removal after its log", () => {
    const rows = card().rows;
    expect(sortRows(rows, ["top"]).map((r) => `${String(r.entry_id)}${r.kind}`)).toEqual([
      "1logged",
      "2logged",
      "2removed",
      "3logged",
    ]);
    expect(sortRows(rows, ["-top"]).at(-1)?.entry_id).toBe(3);
  });

  it("toggles the first key and keeps the others", () => {
    expect(nextSort(["date", "developer"], "minutes")).toEqual(["minutes", "date", "developer"]);
    expect(nextSort(["minutes", "date"], "minutes")).toEqual(["-minutes", "date"]);
    expect(nextSort(["-minutes", "date"], "minutes")).toEqual(["minutes", "date"]);
  });
});
