import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Group, Page, Project } from "../api/gitlab";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { TAGS, auditor, manager, tag, tagDetail } from "../test/manager";
import { SCOPE_ACTIVE, SCOPE_PROPOSED, exclusions } from "../test/scope";

type Routes = Record<string, (init?: RequestInit) => Response>;

const payments = tag(1, "team", "payments", { description: "Payments team" });
const costCentre = tag(2, "cost-center", "cc-104", {
  color: null,
  groups: 0,
  projects: 2,
});

function api(user: CurrentUser, routes: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [TAGS]: () => json({ items: [payments, costCentre] }),
    [SCOPE_ACTIVE]: () => json(exclusions([])),
    [SCOPE_PROPOSED]: () => json(exclusions([])),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function bodyOf(calls: Call[], key: string): unknown {
  const [method, url] = key.split(" ");
  const call = calls.filter((c) => c.url === url && c.init?.method === method).at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("tags", () => {
  it("lists tags with what they cover, never by colour alone", async () => {
    api(auditor);

    renderAt("/manager/tags");

    const table = await screen.findByRole("table", { name: "Tags" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("team=payments");
    expect(rows[1]).toHaveTextContent("Payments team");
    expect(
      within(rows[1] as HTMLElement).getByRole("link", {
        name: "team=payments",
      }),
    ).toHaveAttribute("href", "/manager/tags/1");
    expect(
      within(rows[1] as HTMLElement).getByRole("link", {
        name: "Overview for team=payments",
      }),
    ).toHaveAttribute("href", "/manager?tag=team%3Apayments");
    expect(rows[2]).toHaveTextContent("cost-center=cc-104None02");
    // Without tags:write there is nothing to change.
    expect(screen.queryByRole("form", { name: "New tag" })).not.toBeInTheDocument();
  });

  it("creates a tag after checking it", async () => {
    const created = tag(3, "team", "web");
    const calls = api(manager, {
      "POST /api/v1/tags": () => json(created, 201),
    });
    const user = userEvent.setup();
    renderAt("/manager/tags");

    const form = await screen.findByRole("form", { name: "New tag" });
    await user.type(within(form).getByLabelText("Key"), "Team");
    await user.type(within(form).getByLabelText("Value"), "web");
    await user.click(within(form).getByRole("button", { name: "Create tag" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Use lowercase letters, digits",
    );
    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);

    await user.clear(within(form).getByLabelText("Key"));
    await user.type(within(form).getByLabelText("Key"), "team");
    await user.type(within(form).getByLabelText("Description (optional)"), "Web team");
    await user.click(within(form).getByRole("button", { name: "Create tag" }));

    expect(
      await screen.findByText("Tag team=web created. Assign it to groups or projects next."),
    ).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/tags")).toEqual({
      key: "team",
      value: "web",
      description: "Web team",
    });
  });

  it("explains a tag that already exists", async () => {
    api(manager, {
      "POST /api/v1/tags": () =>
        json(
          {
            detail: {
              code: "tag_exists",
              message: "team=payments exists",
              reasons: ["tag_exists"],
            },
          },
          409,
        ),
    });
    const user = userEvent.setup();
    renderAt("/manager/tags");

    const form = await screen.findByRole("form", { name: "New tag" });
    await user.type(within(form).getByLabelText("Key"), "team");
    await user.type(within(form).getByLabelText("Value"), "payments");
    await user.click(within(form).getByRole("button", { name: "Create tag" }));

    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent("Not possible now: team=payments exists.");
    expect(alert).toHaveTextContent("a tag with that key and value already exists");
  });
});

describe("one tag", () => {
  it("shows where it is assigned and removes an assignment", async () => {
    const calls = api(manager, {
      "GET /api/v1/tags/1": () => json(tagDetail(payments)),
      "POST /api/v1/tags/1/unassign": () => json(tagDetail(payments, { assignments: [] })),
    });
    const user = userEvent.setup();
    renderAt("/manager/tags/1");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Tag team=payments",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("6 projects, through 1 groups and direct assignments")).toBeVisible();
    expect(screen.getByRole("link", { name: "acme/payments" })).toHaveAttribute(
      "href",
      "/inventory/groups/110",
    );
    await user.click(screen.getByRole("button", { name: "Remove from acme/payments" }));

    expect(await screen.findByText("team=payments removed from acme/payments.")).toBeVisible();
    expect(bodyOf(calls, "POST /api/v1/tags/1/unassign")).toEqual({
      targets: [{ type: "group", gitlab_id: 110 }],
    });
  });

  it("assigns it to a group found by path, listing a conflict's paths", async () => {
    const group: Group = {
      id: 5,
      gitlab_id: 115,
      name: "web",
      full_path: "acme/web",
      parent_gitlab_id: 100,
      visibility: "internal",
      state: "active",
      first_seen_at: "2026-10-28T06:00:00Z",
      last_seen_at: "2026-10-29T06:00:00Z",
      last_error: null,
      last_error_at: null,
    };
    const groups: Page<Group> = { items: [group], next_after_id: null };
    api(manager, {
      "GET /api/v1/tags/1": () => json(tagDetail(payments)),
      "GET /api/v1/gitlab/groups?search=acme%2Fweb&state=active&monitoring=all&limit=100": () =>
        json(groups),
      "POST /api/v1/tags/1/assign": () =>
        json(
          {
            detail: {
              code: "key_conflict",
              message: "acme/web already has another team",
              reasons: ["acme/web"],
            },
          },
          409,
        ),
    });
    const user = userEvent.setup();
    renderAt("/manager/tags/1");

    await user.type(await screen.findByLabelText("Find a group by path"), "acme/web");
    await user.click(screen.getByRole("button", { name: "Search groups" }));
    await user.click(await screen.findByRole("radio", { name: /acme\/web/ }));
    await user.click(screen.getByRole("button", { name: "Assign team=payments" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("acme/web already has another team");
    expect(within(alert).getByText("acme/web")).toBeInTheDocument();
  });

  it("is read-only without tags:write", async () => {
    api(auditor, { "GET /api/v1/tags/1": () => json(tagDetail(payments)) });
    renderAt("/manager/tags/1");
    expect(await screen.findByRole("link", { name: "acme/payments" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Edit tag" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Assign" })).not.toBeInTheDocument();
  });
});

function project(id: number, path: string, extra: Partial<Project> = {}): Project {
  return {
    id,
    gitlab_id: id + 100,
    name: path.split("/").at(-1) ?? path,
    path_with_namespace: path,
    namespace_gitlab_id: 1,
    namespace_kind: "group",
    visibility: "private",
    archived: false,
    default_branch: "main",
    state: "active",
    first_seen_at: "2026-10-28T06:00:00Z",
    last_seen_at: "2026-10-29T06:00:00Z",
    last_error: null,
    last_error_at: null,
    ...extra,
  };
}

describe("tags in the inventory", () => {
  const projects: Page<Project> = {
    items: [
      project(1, "acme/payments/api", {
        tags: [
          {
            key: "team",
            value: "payments",
            via: "group",
            from_path: "acme/payments",
          },
        ],
      }),
      project(2, "acme/web/site"),
    ],
    next_after_id: null,
  };

  it("shows each project's tags and where they come from", async () => {
    api(analyst, {
      "GET /api/v1/gitlab/projects?limit=100": () => json(projects),
    });
    renderAt("/inventory");
    const table = await screen.findByRole("table", { name: "GitLab projects" });
    const tags = within(table).getByRole("list", { name: "Tags" });
    expect(tags).toHaveTextContent("team=payments (from group acme/payments)");
  });

  it("filters by a tag typed as key=value", async () => {
    const calls = api(analyst, {
      "GET /api/v1/gitlab/projects?limit=100": () => json(projects),
      "GET /api/v1/gitlab/projects?tag=team%3Apayments&limit=100": () =>
        json({ items: [projects.items[0]], next_after_id: null }),
    });
    const user = userEvent.setup();
    const router = renderAt("/inventory");

    const form = await screen.findByRole("form", { name: "Filter projects" });
    await user.type(within(form).getByLabelText("Tag"), "team=payments");
    await user.click(within(form).getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("Showing 1 projects.")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?tag=team%3Apayments");
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/gitlab/projects?tag=team%3Apayments&limit=100",
    );
  });

  it("lets a manager tag selected projects without scope actions", async () => {
    const calls = api(manager, {
      "GET /api/v1/gitlab/projects?limit=100": () => json(projects),
      "POST /api/v1/tags/2/assign": () => json(tagDetail(costCentre)),
    });
    const user = userEvent.setup();
    renderAt("/inventory");

    await user.click(await screen.findByRole("checkbox", { name: "Select acme/web/site" }));
    const bar = screen.getByRole("group", { name: "Selected projects" });
    expect(within(bar).queryByRole("button", { name: "Exclude from monitoring" })).toBeNull();
    await user.click(within(bar).getByRole("button", { name: "Tag…" }));
    const form = screen.getByRole("group", { name: "Tag 1 selected item" });
    await user.selectOptions(
      await within(form).findByLabelText("Tag"),
      await within(form).findByRole("option", { name: "cost-center=cc-104" }),
    );
    await user.click(within(form).getByRole("button", { name: "Add tag" }));

    expect(await screen.findByText("cost-center=cc-104 added to 1 item.")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/tags/2/assign")).toEqual({
      targets: [{ type: "project", gitlab_id: 102 }],
    });
  });

  it("offers no selection to a reader who can neither tag nor exclude", async () => {
    api(auditor, {
      "GET /api/v1/gitlab/projects?limit=100": () => json(projects),
    });
    renderAt("/inventory");
    await screen.findByRole("table", { name: "GitLab projects" });
    expect(screen.queryByRole("checkbox", { name: /^Select/ })).not.toBeInTheDocument();
  });
});
