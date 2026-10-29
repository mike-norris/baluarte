import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import {
  exportFilename,
  findingsExportLink,
  readExportForm,
  validateExport,
} from "../evidence/scope";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { finding, findingDetail } from "../test/findings";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const EXPORT = "POST /api/v1/evidence/exports";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: [...analyst.permissions, "evidence:export"],
};

function api(
  routes: Record<string, (init?: RequestInit) => Response | Promise<Response>>,
  user: CurrentUser = auditor,
): Call[] {
  return mockApi({
    [ME]: () => json(user),
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

function exported(body = '{"ok":true}', headers: Record<string, string> = {}): Response {
  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Content-Disposition":
        'attachment; filename="baluarte-evidence-20261029T070000Z-abcd1234.json"',
      "X-Baluarte-Export-Id": "abcd1234ef",
      ...headers,
    },
  });
}

function requestBody(calls: Call[]): unknown {
  const call = calls.find((item) => item.url === "/api/v1/evidence/exports");
  return JSON.parse(String(call?.init?.body));
}

let created: Blob[];
let saved: string[];

beforeEach(() => {
  created = [];
  saved = [];
  URL.createObjectURL = vi.fn((blob: Blob) => {
    created.push(blob);
    return "blob:export";
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

describe("evidence export page", () => {
  it("explains the permission instead of showing the form to users who cannot export", async () => {
    api({}, analyst);
    renderAt("/evidence");

    expect(
      await screen.findByText(/permission, which the admin and auditor roles have/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Export evidence" })).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Data freshness" })).toBeInTheDocument();
  });

  it("requires a scope or an explicit 'everything', and sends nothing otherwise", async () => {
    const user = userEvent.setup();
    const calls = api({ [EXPORT]: () => exported() });
    renderAt("/evidence");

    await user.click(await screen.findByRole("button", { name: "Export" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Choose at least one scope field, or tick “Export everything”.",
    );
    expect(calls.some((call) => call.url === "/api/v1/evidence/exports")).toBe(false);
  });

  it("exports everything with only all=true, ignoring the disabled scope fields", async () => {
    const user = userEvent.setup();
    const calls = api({ [EXPORT]: () => exported() });
    renderAt("/evidence?policy=corporate-default");

    await user.click(await screen.findByRole("checkbox", { name: /Export everything/ }));
    expect(screen.getByRole("textbox", { name: "Policy name" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Export" }));

    expect(await screen.findByText(/^Downloaded /)).toBeInTheDocument();
    expect(requestBody(calls)).toEqual({
      format: "json",
      scope: { all: true },
    });
  });

  it("prefills from the URL and sends exactly the set fields with the CSRF token", async () => {
    const user = userEvent.setup();
    const calls = api({
      [EXPORT]: () =>
        exported("PK", {
          "Content-Type": "application/zip",
          "Content-Disposition": 'attachment; filename="baluarte-evidence-x.zip"',
        }),
    });
    renderAt(
      "/evidence?projects=105,230&policy=corporate-default&rule=project-visibility" +
        "&finding_state=open&since=2026-10-01T00:00:00Z&until=2026-10-29T00:00Z&format=csv",
    );

    expect(await screen.findByRole("textbox", { name: "Project GitLab IDs" })).toHaveValue(
      "105, 230",
    );
    expect(screen.getByLabelText("Evaluated from (UTC)")).toHaveValue("2026-10-01T00:00");
    expect(screen.getByRole("radio", { name: "ZIP of CSV files" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Export" }));

    expect(await screen.findByText(/^Downloaded baluarte-evidence-x\.zip/)).toBeInTheDocument();
    expect(requestBody(calls)).toEqual({
      format: "csv",
      scope: {
        projects: [105, 230],
        policy: "corporate-default",
        rule: "project-visibility",
        finding_state: "open",
        since: "2026-10-01T00:00:00Z",
        until: "2026-10-29T00:00:00Z",
      },
    });
    const call = calls.find((item) => item.url === "/api/v1/evidence/exports");
    expect(call?.init?.method).toBe("POST");
    expect(call?.init?.credentials).toBe("same-origin");
    expect(call?.init?.headers).toMatchObject({
      "X-CSRF-Token": auditor.csrf_token,
      "Content-Type": "application/json",
    });
  });

  it("offers the file only after every byte arrived, under the server's name", async () => {
    const user = userEvent.setup();
    api({ [EXPORT]: () => exported('{"rows":[1,2,3]}') });
    renderAt("/evidence?rule=project-visibility");

    await user.click(await screen.findByRole("button", { name: "Export" }));

    expect(
      await screen.findByText(
        "Downloaded baluarte-evidence-20261029T070000Z-abcd1234.json (16 bytes). " +
          "Verify it before relying on it.",
      ),
    ).toBeInTheDocument();
    expect(saved).toEqual(["baluarte-evidence-20261029T070000Z-abcd1234.json"]);
    expect(created).toHaveLength(1);
    expect(await created[0]?.text()).toBe('{"rows":[1,2,3]}');
    expect(screen.getByText("abcd1234ef")).toBeInTheDocument();
    expect(
      screen.getByText(
        "baluarte-verify-export baluarte-evidence-20261029T070000Z-abcd1234.json " +
          "--manifest-sha256 <manifest_sha256>",
      ),
    ).toBeInTheDocument();
  });

  it("shows the API's reason when it refuses the scope", async () => {
    const user = userEvent.setup();
    api({
      [EXPORT]: () => json({ detail: "Projects not in the inventory: 999" }, 422),
    });
    renderAt("/evidence?projects=999");

    await user.click(await screen.findByRole("button", { name: "Export" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The export was refused: Projects not in the inventory: 999",
    );
    expect(saved).toEqual([]);
  });

  it("reads the API's validation messages", async () => {
    const user = userEvent.setup();
    api({
      [EXPORT]: () =>
        json(
          {
            detail: [{ msg: "Value error, since must be before until", loc: [] }],
          },
          422,
        ),
    });
    renderAt("/evidence?rule=r");

    await user.click(await screen.findByRole("button", { name: "Export" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The export was refused: since must be before until",
    );
  });

  it.each([
    [403, { detail: "Insufficient permissions" }, "You do not have permission to export evidence."],
    [403, { detail: "Missing or invalid CSRF token" }, "security token was rejected"],
    [409, { detail: "Nothing has been collected yet" }, "Nothing has been collected yet"],
  ])("explains a %s response", async (status, body, message) => {
    const user = userEvent.setup();
    api({ [EXPORT]: () => json(body, status) });
    renderAt("/evidence?rule=r");

    await user.click(await screen.findByRole("button", { name: "Export" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("returns to sign-in when the session ended", async () => {
    const user = userEvent.setup();
    let signedIn = true;
    mockApi({
      [ME]: () => (signedIn ? json(auditor) : json({}, 401)),
      [FRESHNESS]: () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
      [EXPORT]: () => {
        signedIn = false;
        return json({ detail: "Not authenticated" }, 401);
      },
    });
    renderAt("/evidence?rule=r");

    await user.click(await screen.findByRole("button", { name: "Export" }));

    expect(await screen.findByRole("heading", { name: "Sign in to Baluarte" })).toBeInTheDocument();
  });

  it("cancels a running export and saves nothing", async () => {
    const user = userEvent.setup();
    api({
      [EXPORT]: (init) => {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode("12345"));
            init?.signal?.addEventListener("abort", () => {
              controller.error(new DOMException("Aborted", "AbortError"));
            });
          },
        });
        return new Response(body, { status: 200 });
      },
    });
    renderAt("/evidence?rule=r");

    await user.click(await screen.findByRole("button", { name: "Export" }));
    expect(await screen.findByText("5 bytes received")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Cancel export" }));

    expect(await screen.findByText("Export cancelled. Nothing was saved.")).toBeInTheDocument();
    expect(saved).toEqual([]);
    expect(created).toEqual([]);
  });

  it("checks the time window and paths before sending", async () => {
    const calls = api({ [EXPORT]: () => exported() });
    renderAt("/evidence?group=/platform/&projects=12,abc");

    await screen.findByRole("button", { name: "Export" });
    fireEvent.change(screen.getByLabelText("Evaluated from (UTC)"), {
      target: { value: "2026-10-29T00:00" },
    });
    fireEvent.change(screen.getByLabelText("Evaluated before (UTC)"), {
      target: { value: "2026-10-01T00:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("without leading or trailing slashes");
    expect(alert).toHaveTextContent("whole numbers above zero");
    expect(alert).toHaveTextContent("must be before its end");
    expect(calls.some((call) => call.url === "/api/v1/evidence/exports")).toBe(false);
  });
});

describe("export links", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("links the findings view to a prefilled export and says severity is not applied", async () => {
    api({
      "GET /api/v1/findings?state=open&severity=high&project=105&limit=50": () =>
        json({ items: [finding(1)], next_after_id: null }),
    });
    renderAt("/findings?state=open&severity=high&project=105");

    const link = await screen.findByRole("link", {
      name: "Export evidence for this view",
    });
    expect(link).toHaveAttribute("href", "/evidence?projects=105&finding_state=open");
    expect(screen.getByText(/cannot filter by severity/)).toBeInTheDocument();
  });

  it("hides the link from users who cannot export", async () => {
    api(
      {
        "GET /api/v1/findings?limit=50": () => json({ items: [], next_after_id: null }),
      },
      analyst,
    );
    renderAt("/findings");

    await screen.findByText(/No findings yet/);
    expect(
      screen.queryByRole("link", { name: "Export evidence for this view" }),
    ).not.toBeInTheDocument();
  });

  it("links a finding to an export of its project, policy and rule", async () => {
    const detail = findingDetail(4);
    api({
      "GET /api/v1/findings/4": () => json(detail),
      "GET /api/v1/findings/4/evaluations?limit=50": () =>
        json({ items: [], next_before_id: null }),
      "GET /api/v1/gitlab/projects/105/observations?before_id=10&limit=1": () =>
        json({ items: [], next_before_id: null }),
    });
    renderAt("/findings/4");

    const link = await screen.findByRole("link", {
      name: "Export evidence for this project and rule",
    });
    const params = new URLSearchParams(link.getAttribute("href")?.split("?")[1]);
    expect(Object.fromEntries(params)).toEqual({
      projects: String(detail.project.gitlab_id),
      policy: detail.policy.name,
      rule: detail.rule_id,
    });
  });
});

describe("export scope helpers", () => {
  it.each([
    ['attachment; filename="baluarte-evidence-1.json"', "json", "baluarte-evidence-1.json"],
    ['attachment; filename="../../etc/passwd"', "json", "passwd.json"],
    ['attachment; filename="a b<c>.zip"', "csv", "a_b_c_.zip"],
    ['attachment; filename=".hidden.json"', "json", "hidden.json"],
    ['attachment; filename="report.exe"', "json", "report.exe.json"],
    ["attachment; filename=plain.zip", "csv", "plain.zip"],
    [null, "csv", "baluarte-evidence.zip"],
    ['attachment; filename="///"', "json", "baluarte-evidence.json"],
  ] as const)("sanitises %s", (header, format, expected) => {
    expect(exportFilename(header, format)).toBe(expected);
  });

  it("drops invalid URL values instead of sending them", () => {
    const form = readExportForm(
      new URLSearchParams("outcome=maybe&finding_state=closed&since=yesterday&format=pdf"),
    );
    expect(form).toMatchObject({
      outcome: "",
      findingState: "",
      since: "",
      format: "json",
    });
  });

  it("deduplicates project IDs and keeps only set fields", () => {
    const result = validateExport({
      ...readExportForm(new URLSearchParams()),
      projects: "5, 5 7",
      outcome: "unknown",
    });
    expect(result).toEqual({
      request: {
        format: "json",
        scope: { projects: [5, 7], outcome: "unknown" },
      },
    });
  });

  it("maps findings filters onto export fields", () => {
    expect(findingsExportLink({ rule: "r", outcome: "fail" })).toEqual({
      href: "/evidence?rule=r&outcome=fail",
      dropped: [],
    });
  });
});
