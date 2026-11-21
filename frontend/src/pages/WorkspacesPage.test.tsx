import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceInfo, WorkspaceList } from "../api/workspace";
import { scopeHeaders } from "../api/workspaceChoice";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
  workspace: "default",
  workspaces: [
    { slug: "default", name: "Default", roles: ["admin"] },
    { slug: "acme", name: "Acme", roles: ["auditor"] },
  ],
};

const home: WorkspaceInfo = {
  slug: "default",
  name: "Default",
  default: true,
  roles: ["admin"],
  group_roles: null,
  connections: [{ id: 1, name: null, url: "https://gitlab.example.test", primary: true }],
};
const acme: WorkspaceInfo = {
  slug: "acme",
  name: "Acme",
  default: false,
  roles: ["auditor"],
  group_roles: { admin: ["acme-admins"], auditor: ["acme-auditors"] },
  connections: [
    { id: 2, name: "Acme EU", url: "https://eu.acme.test", primary: false },
    { id: 3, name: null, url: "https://us.acme.test", primary: false },
  ],
};

function list(can_manage = true): WorkspaceList {
  return { workspaces: [home, acme], can_manage };
}

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, extra: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/workspaces": () => json(list()),
    ...extra,
  });
}

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function body(call: Call | undefined): unknown {
  return JSON.parse(String(call?.init?.body ?? "null"));
}

const assign = vi.fn();

beforeEach(() => {
  window.localStorage.clear();
  vi.stubGlobal("location", { ...window.location, assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
  assign.mockReset();
});

describe("WorkspacesPage", () => {
  it("lists workspaces with their connections and groups", async () => {
    api(admin);
    renderAt("/workspaces");

    const card = await screen.findByRole("region", { name: "Acme" });
    expect(within(card).getByText("Connections: Acme EU, us.acme.test")).toBeInTheDocument();
    expect(within(card).getByText("acme-admins")).toBeInTheDocument();
    expect(within(card).getByText("You: auditor")).toBeInTheDocument();
    const main = screen.getByRole("region", { name: "Default" });
    expect(within(main).getByText(/Settings page/)).toBeInTheDocument();
  });

  it("creates a workspace with groups per role", async () => {
    const created: WorkspaceInfo = { ...acme, slug: "beta", name: "Beta", connections: [] };
    const calls = api(admin, { "POST /api/v1/workspaces": () => json(created, 201) });
    renderAt("/workspaces");
    const form = await screen.findByRole("form", { name: "New workspace" });

    await userEvent.type(within(form).getByLabelText("Name"), "Beta");
    await userEvent.type(within(form).getByLabelText("Short name"), "beta");
    await userEvent.type(
      within(form).getByLabelText(/Analysts/),
      "beta-analysts{enter}beta-readers",
    );
    await userEvent.click(within(form).getByRole("button", { name: "Create workspace" }));

    expect(await screen.findByText(/Workspace Beta created/)).toBeInTheDocument();
    expect(body(calls.find((c) => c.init?.method === "POST"))).toEqual({
      slug: "beta",
      name: "Beta",
      group_roles: { analyst: ["beta-analysts", "beta-readers"] },
    });
  });

  it("refuses a workspace nobody could work in", async () => {
    const calls = api(admin);
    renderAt("/workspaces");
    const form = await screen.findByRole("form", { name: "New workspace" });

    await userEvent.type(within(form).getByLabelText("Name"), "Beta");
    await userEvent.type(within(form).getByLabelText("Short name"), "Beta Corp");
    await userEvent.click(within(form).getByRole("button", { name: "Create workspace" }));

    expect(await within(form).findByText(/lowercase letters/)).toBeInTheDocument();
    expect(within(form).getByText(/nobody can work/)).toBeInTheDocument();
    expect(calls.some((c) => c.init?.method === "POST")).toBe(false);
  });

  it("hides management from people who are not deployment administrators", async () => {
    api(admin, { "GET /api/v1/workspaces": () => json(list(false)) });
    renderAt("/workspaces");

    await screen.findByRole("region", { name: "Acme" });
    expect(screen.queryByRole("form", { name: "New workspace" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });
});

describe("WorkspaceSwitcher", () => {
  it("switches workspace and connection, and sends them with every request", async () => {
    const calls = api(admin);
    renderAt("/workspaces");

    const select = await screen.findByLabelText("Workspace and connection");
    expect(within(select).getByRole("option", { name: "Acme: Acme EU" })).toBeInTheDocument();
    await userEvent.selectOptions(select, "acme:3");

    expect(assign).toHaveBeenCalledWith("/");
    expect(scopeHeaders()).toEqual({
      "X-Baluarte-Workspace": "acme",
      "X-Baluarte-Connection": "3",
    });
    expect(calls.length).toBeGreaterThan(0);
  });

  it("is hidden when there is nothing to choose", async () => {
    api(
      { ...admin, workspaces: [{ slug: "default", name: "Default", roles: ["admin"] }] },
      { "GET /api/v1/workspaces": () => json({ workspaces: [home], can_manage: true }) },
    );
    renderAt("/workspaces");

    await screen.findByRole("region", { name: "Default" });
    expect(screen.queryByLabelText("Workspace and connection")).not.toBeInTheDocument();
  });

  it("forgets a workspace the person can no longer use", async () => {
    window.localStorage.setItem("baluarte.workspace", "gone");
    let attempts = 0;
    api(admin, {
      "GET /api/v1/auth/me": () => {
        attempts += 1;
        return scopeHeaders()["X-Baluarte-Workspace"] === "gone"
          ? json({ detail: "You have no role in this workspace" }, 403)
          : json(admin);
      },
    });
    renderAt("/workspaces");

    await screen.findByRole("region", { name: "Acme" });
    expect(attempts).toBeGreaterThanOrEqual(2);
    expect(window.localStorage.getItem("baluarte.workspace")).toBeNull();
  });
});
