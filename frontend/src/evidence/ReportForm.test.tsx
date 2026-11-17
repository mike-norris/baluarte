import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { validateReport, type ReportForm } from "./report";

const REPORT = "POST /api/v1/evidence/reports";
const FORM = "Download the auditor report";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:read", "findings:read", "inventory:read", "evidence:export"],
};

let saved: string[] = [];
let blobs: Blob[] = [];

beforeEach(() => {
  saved = [];
  blobs = [];
  URL.createObjectURL = vi.fn((blob: Blob) => {
    blobs.push(blob);
    return "blob:report";
  });
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

function api(
  routes: Record<string, (init?: RequestInit) => Response>,
  user: CurrentUser = auditor,
): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

function renderAt(path = "/evidence") {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function pdf(): Response {
  return new Response("%PDF-1.4", {
    status: 200,
    headers: {
      "Content-Type": "application/pdf",
      "Content-Disposition":
        'attachment; filename="baluarte-soc2-20260701T000000Z-20261001T000000Z.pdf"',
      "X-Baluarte-Report-Id": "rep-1",
    },
  });
}

function body(calls: Call[]): unknown {
  const call = calls.find((item) => item.url === "/api/v1/evidence/reports");
  return JSON.parse(String(call?.init?.body));
}

async function fill(user: ReturnType<typeof userEvent.setup>, groups = "") {
  const form = await screen.findByRole("form", { name: FORM });
  await user.selectOptions(within(form).getByLabelText("Framework"), "SOC 2");
  await user.type(within(form).getByLabelText("From (UTC)"), "2026-07-01T00:00");
  await user.type(within(form).getByLabelText("Up to (UTC)"), "2026-10-01T00:00");
  if (groups) await user.type(within(form).getByLabelText("Group paths"), groups);
  await user.click(within(form).getByRole("button", { name: "Download PDF" }));
  return form;
}

const blank: ReportForm = {
  framework: "soc2",
  from: "2026-07-01T00:00",
  to: "2026-10-01T00:00",
  groups: "",
};

describe("report request", () => {
  it("builds the request, with groups only when named", () => {
    expect(validateReport(blank)).toEqual({
      request: { framework: "soc2", from: "2026-07-01T00:00:00Z", to: "2026-10-01T00:00:00Z" },
    });
    expect(validateReport({ ...blank, groups: "acme/payments, acme/platform" })).toEqual({
      request: {
        framework: "soc2",
        from: "2026-07-01T00:00:00Z",
        to: "2026-10-01T00:00:00Z",
        groups: ["acme/payments", "acme/platform"],
      },
    });
  });

  it("explains every problem", () => {
    expect(validateReport({ ...blank, framework: "", to: "2026-06-01T00:00" })).toEqual({
      errors: ["Choose the framework the report is for.", "The period must end after it starts."],
    });
    expect(validateReport({ ...blank, from: "", to: "" })).toEqual({
      errors: ["Choose when the period starts.", "Choose when the period ends."],
    });
  });
});

describe("auditor report card", () => {
  it("downloads the PDF under the server's name", async () => {
    const user = userEvent.setup();
    const calls = api({ [REPORT]: () => pdf() });
    renderAt();

    await fill(user, "acme/payments");

    expect(
      await screen.findByText("Downloaded baluarte-soc2-20260701T000000Z-20261001T000000Z.pdf."),
    ).toBeInTheDocument();
    expect(saved).toEqual(["baluarte-soc2-20260701T000000Z-20261001T000000Z.pdf"]);
    expect(blobs[0]?.type).toBe("application/pdf");
    expect(screen.getByText("rep-1")).toBeInTheDocument();
    expect(body(calls)).toEqual({
      framework: "soc2",
      from: "2026-07-01T00:00:00Z",
      to: "2026-10-01T00:00:00Z",
      groups: ["acme/payments"],
    });
    const call = calls.find((item) => item.url === "/api/v1/evidence/reports");
    expect(new Headers(call?.init?.headers).get("X-CSRF-Token")).toBe(auditor.csrf_token);
  });

  it("does not send a report the form says is invalid", async () => {
    const user = userEvent.setup();
    const calls = api({});
    renderAt();

    const form = await screen.findByRole("form", { name: FORM });
    await user.click(within(form).getByRole("button", { name: "Download PDF" }));

    expect(await screen.findByText("The report was not requested:")).toBeInTheDocument();
    expect(screen.getByText("Choose the framework the report is for.")).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/v1/evidence/reports")).toBe(false);
  });

  it("shows a missing evaluation run inline, with its reasons", async () => {
    const user = userEvent.setup();
    api({
      [REPORT]: () =>
        json(
          {
            detail: {
              code: "no_evaluation_run",
              message: "No evaluation run had finished by the end of the period",
              reasons: ["Results are as of the period's end, so a package needs one."],
            },
          },
          409,
        ),
    });
    renderAt();

    await fill(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No evaluation run had finished by the end of the period");
    expect(within(alert).getByRole("list", { name: "Reasons" })).toHaveTextContent(
      "Results are as of the period's end",
    );
    expect(saved).toEqual([]);
  });

  it("shows an invalid scope inline", async () => {
    const user = userEvent.setup();
    api({ [REPORT]: () => json({ detail: "Groups not in the inventory: nowhere" }, 422) });
    renderAt();

    await fill(user, "nowhere");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Groups not in the inventory: nowhere",
    );
  });

  it("is offered only with evidence:export", async () => {
    api({}, { ...auditor, permissions: ["evidence:read", "evidence:package"] });
    renderAt();

    expect(
      await screen.findByRole("form", { name: "Create an audit package" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: FORM })).not.toBeInTheDocument();
  });

  it("is not offered on phones", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width: 767.98px"),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    api({});
    renderAt();

    expect(
      await screen.findByText("Evidence exports are made on a larger screen."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: FORM })).not.toBeInTheDocument();
  });
});
