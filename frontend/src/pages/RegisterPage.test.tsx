import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RegisterIssue, RegisterPage, Rule } from "../api/register";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const writer: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "register:write"],
};
const reader: CurrentUser = {
  ...analyst,
  permissions: analyst.permissions.filter((p) => p !== "register:write"),
};

function issue(iid: number, extra: Partial<RegisterIssue> = {}): RegisterIssue {
  return {
    project_id: 5,
    project: "acme/api",
    connection_id: 1,
    iid,
    issue_type: "issue",
    title: `Issue ${String(iid)}`,
    state: "opened",
    labels: ["security"],
    author: "ada",
    assignees: ["grace"],
    milestone: null,
    web_url: `https://gitlab.example.test/acme/api/-/issues/${String(iid)}`,
    confidential: false,
    opened_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-11-01T10:00:00Z",
    closed_at: null,
    read_at: "2026-11-21T10:00:00Z",
    category: "security_finding",
    excluded: false,
    why: [
      {
        kind: "rule",
        rule_id: 1,
        rule_name: "Security issues",
        decision_id: null,
        reason: null,
        decided_by: null,
        decided_at: null,
      },
    ],
    ...extra,
  };
}

function page(extra: Partial<RegisterPage> = {}): RegisterPage {
  return {
    items: [issue(1), issue(2)],
    total: 2,
    pending: [],
    active: true,
    last_read_at: "2026-11-21T10:00:00Z",
    ...extra,
  };
}

const rule: Rule = {
  id: 1,
  name: "Security issues",
  category: "security_finding",
  labels: ["security"],
  project_ids: [],
  projects: [],
  issue_types: [],
  created_by: "user:1",
  created_at: "2026-11-20T10:00:00Z",
  ended_by: null,
  ended_at: null,
};

type Routes = Record<string, (init?: RequestInit) => Response>;

/** Like mockApi, matching on the path so the register's query strings need not be spelled out. */
function api(user: CurrentUser, extra: Routes = {}): Call[] {
  const routes: Routes = {
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/register/issues": () => json(page()),
    "GET /api/v1/register/rules": () => json({ rules: [rule] }),
    "GET /api/v1/gitlab/projects": () =>
      json({
        items: [{ id: 5, gitlab_id: 42, path_with_namespace: "acme/api", name: "api" }],
        next_after_id: null,
      }),
    ...extra,
  };
  const calls: Call[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input.toString();
    calls.push({ url, init });
    const method = init?.method ?? "GET";
    const handler = routes[`${method} ${url}`] ?? routes[`${method} ${url.split("?")[0] ?? ""}`];
    return handler
      ? Promise.resolve(handler(init))
      : Promise.reject(new TypeError(`unexpected fetch: ${method} ${url}`));
  });
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function body(call: Call | undefined): unknown {
  return JSON.parse(String(call?.init?.body ?? "null"));
}

describe("RegisterPage", () => {
  it("lists auditable issues with their category and why", async () => {
    api(reader);
    renderAt("/register");

    const row = (await screen.findByRole("link", { name: "acme/api#1" })).closest("tr");
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement);
    expect(cells.getByText("Security finding")).toBeInTheDocument();
    expect(cells.getByText("Rule: Security issues")).toBeInTheDocument();
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Exclude" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Include an issue" })).not.toBeInTheDocument();
  });

  it("explains an empty register", async () => {
    api(reader, {
      "GET /api/v1/register/issues": () => json(page({ items: [], total: 0, active: false })),
    });
    renderAt("/register");

    expect(await screen.findByText("No register yet")).toBeInTheDocument();
  });

  it("sends filters to the API", async () => {
    const calls = api(reader);
    renderAt("/register");
    const form = await screen.findByRole("form", { name: "Filter the register" });

    await userEvent.selectOptions(within(form).getByLabelText("Category"), "exception");
    await userEvent.type(within(form).getByLabelText("Open between"), "2026-01-01");
    await userEvent.click(within(form).getByLabelText("Show excluded issues"));
    await userEvent.click(within(form).getByRole("button", { name: "Apply filters" }));

    await screen.findAllByRole("link", { name: /acme\/api#/ });
    const last = calls.filter((c) => c.url.includes("/register/issues")).at(-1);
    expect(last?.url).toContain("category=exception");
    expect(last?.url).toContain("period_start=2026-01-01T00%3A00%3A00Z");
    expect(last?.url).toContain("include_excluded=true");
  });

  it("excludes an issue with a reason", async () => {
    const calls = api(writer, {
      "POST /api/v1/register/decisions": () => json({ id: 7 }, 201),
    });
    renderAt("/register");
    const row = (await screen.findByRole("link", { name: "acme/api#2" })).closest("tr");

    await userEvent.click(within(row as HTMLElement).getByRole("button", { name: "Exclude" }));
    const dialog = await screen.findByRole("dialog", { name: "Exclude acme/api#2" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "too short");
    await userEvent.click(within(dialog).getByRole("button", { name: "Exclude" }));
    expect(within(dialog).getByText(/at least 20 characters/)).toBeInTheDocument();

    await userEvent.type(within(dialog).getByLabelText("Reason"), " and now long enough to count");
    await userEvent.click(within(dialog).getByRole("button", { name: "Exclude" }));

    expect(await screen.findByText("acme/api#2 excluded.")).toBeInTheDocument();
    expect(body(calls.find((c) => c.init?.method === "POST"))).toEqual({
      project_id: 5,
      iid: 2,
      decision: "exclude",
      reason: "too short and now long enough to count",
    });
  });

  it("ends an exclusion and shows pending inclusions", async () => {
    const excluded = issue(3, {
      excluded: true,
      why: [
        ...issue(3).why,
        {
          kind: "excluded",
          rule_id: null,
          rule_name: null,
          decision_id: 9,
          reason: "Duplicate of issue 1, tracked there",
          decided_by: "user:2",
          decided_at: "2026-11-20T10:00:00Z",
        },
      ],
    });
    const calls = api(writer, {
      "GET /api/v1/register/issues": () =>
        json(
          page({
            items: [excluded],
            total: 1,
            pending: [
              {
                project_id: 5,
                project: "acme/api",
                iid: 40,
                decision_id: 11,
                reason: "Change approval evidence for the release",
                decided_by: "user:1",
                decided_at: "2026-11-21T09:00:00Z",
              },
            ],
          }),
        ),
      "POST /api/v1/register/decisions/9/end": () => json({ id: 9 }),
    });
    renderAt("/register");

    expect(await screen.findByText(/Change approval evidence for the release/)).toBeInTheDocument();
    expect(screen.getByText("Excluded")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "End exclusion" }));

    expect(await screen.findByText(/selection rules decide again/)).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/register/decisions/9/end"))).toBe(true);
  });
});

describe("RegisterRulesPage", () => {
  it("lists rules and adds one", async () => {
    const calls = api(writer, {
      "POST /api/v1/register/rules": () => json({ ...rule, id: 2, name: "Incidents" }, 201),
    });
    renderAt("/register/rules");

    const card = await screen.findByRole("region", { name: "Security issues" });
    expect(within(card).getByText("Labels: any of security")).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "New selection rule" });
    await userEvent.type(within(form).getByLabelText("Name"), "Incidents");
    await userEvent.selectOptions(within(form).getByLabelText("Category"), "remediation");
    await userEvent.click(within(form).getByLabelText("Incident"));
    await userEvent.click(within(form).getByRole("button", { name: "Add rule" }));

    expect(await screen.findByText(/Rule Incidents added/)).toBeInTheDocument();
    expect(body(calls.find((c) => c.init?.method === "POST"))).toEqual({
      name: "Incidents",
      category: "remediation",
      labels: [],
      project_ids: [],
      issue_types: ["incident"],
    });
  });

  it("refuses a rule without criteria", async () => {
    const calls = api(writer);
    renderAt("/register/rules");
    const form = await screen.findByRole("form", { name: "New selection rule" });

    await userEvent.type(within(form).getByLabelText("Name"), "Everything");
    await userEvent.click(within(form).getByRole("button", { name: "Add rule" }));

    expect(within(form).getByText(/at least one label, project or issue type/)).toBeInTheDocument();
    expect(calls.some((c) => c.init?.method === "POST")).toBe(false);
  });
});
