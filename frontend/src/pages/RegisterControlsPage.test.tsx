import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuditPeriod, CoverageRow } from "../api/register";
import { routes } from "../routes";
import { analyst, json } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const writer = {
  ...analyst,
  permissions: [...analyst.permissions, "register:write", "evidence:package"],
};

const period: AuditPeriod = {
  id: 2,
  name: "SOC 2 2026",
  starts_on: "2026-01-01",
  ends_on: "2026-06-30",
  created_by: "user:1",
  created_at: "2026-11-01T00:00:00Z",
  ended_by: null,
  ended_at: null,
};

const row: CoverageRow = {
  framework: "soc2",
  control_id: "CC8.1",
  control_title: "Changes are authorized, tested and approved before release",
  issues: 3,
  proposed: 1,
  in_period: null,
  gaps: { awaiting_review: 1 },
};

const sent: { url: string; body: unknown }[] = [];

function api() {
  sent.length = 0;
  const routes: Record<string, () => Response> = {
    "GET /api/v1/auth/me": () => json(writer),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/register/periods": () => json({ items: [period] }),
    "GET /api/v1/register/coverage": () => json({ period_id: null, rows: [row] }),
    "GET /api/v1/register/coverage?period_id=2": () =>
      json({ period_id: 2, rows: [{ ...row, in_period: 0, gaps: { nothing_in_period: 3 } }] }),
    "POST /api/v1/register/periods": () => json({ ...period, id: 3, name: "ISO 2026" }, 201),
    "POST /api/v1/register/exports": () =>
      new Response("PK", {
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": 'attachment; filename="baluarte-register-control-x.zip"',
          "X-Baluarte-Export-Id": "e".repeat(32),
        },
      }),
  };
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input.toString();
    const method = init?.method ?? "GET";
    if (method !== "GET")
      sent.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
    const handler = routes[`${method} ${url}`];
    return handler ? Promise.resolve(handler()) : Promise.reject(new TypeError(url));
  });
}

let saved: string[] = [];

beforeEach(() => {
  saved = [];
  URL.createObjectURL = vi.fn(() => "blob:package");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage() {
  render(
    <RouterProvider
      router={createMemoryRouter(routes, { initialEntries: ["/register/controls"] })}
    />,
  );
}

describe("RegisterControlsPage", () => {
  it("shows coverage per control, and against a chosen period", async () => {
    api();
    renderPage();

    const table = within(await screen.findByRole("region", { name: "Coverage" }));
    expect(table.getByText("SOC 2 CC8.1")).toBeInTheDocument();
    expect(table.getByText("Awaiting review: 1")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Audit period" }), "2");

    expect(await screen.findByText("Nothing in the period: 3")).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "With evidence in the period" }),
    ).toBeInTheDocument();
  });

  it("adds an audit period", async () => {
    api();
    renderPage();

    const form = within(await screen.findByRole("form", { name: "New audit period" }));
    await userEvent.type(form.getByRole("textbox", { name: "Name" }), "ISO 2026");
    await userEvent.type(form.getByLabelText("Starts on"), "2026-01-01");
    await userEvent.type(form.getByLabelText("Ends on"), "2026-12-31");
    await userEvent.click(form.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(sent).toContainEqual({
        url: "/api/v1/register/periods",
        body: { name: "ISO 2026", starts_on: "2026-01-01", ends_on: "2026-12-31" },
      });
    });
  });

  it("exports the chosen controls for the chosen period", async () => {
    api();
    renderPage();

    await screen.findByRole("region", { name: "Coverage" });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Audit period" }), "2");
    await userEvent.click(await screen.findByRole("checkbox", { name: "Export SOC 2 CC8.1" }));
    await userEvent.click(screen.getByRole("button", { name: "Export 1 control for the period" }));

    expect(
      await screen.findByText(/Downloaded baluarte-register-control-x.zip/),
    ).toBeInTheDocument();
    expect(saved).toEqual(["baluarte-register-control-x.zip"]);
    expect(sent).toContainEqual({
      url: "/api/v1/register/exports",
      body: { controls: [{ framework: "soc2", control_id: "CC8.1" }], period_id: 2 },
    });
  });
});
