import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Connection } from "../api/connections";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness, kind } from "../test/freshness";

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
};

function connection(id: number, extra: Partial<Connection> = {}): Connection {
  return {
    id,
    provider: "gitlab",
    name: null,
    url: "https://gitlab.example.test",
    primary: true,
    source: "deployment",
    paused: false,
    token: "configured",
    timeout_seconds: 15,
    sync_interval_minutes: 360,
    allow_private_network: false,
    version: "17.5.1",
    edition: "ee",
    created_at: "2026-10-01T00:00:00Z",
    health: "fresh",
    collections: { capabilities: kind("fresh"), groups: kind("fresh"), projects: kind("fresh") },
    ...extra,
  };
}

const primary = connection(1);
const second = connection(2, {
  name: "EU GitLab",
  url: "https://eu.example.test",
  primary: false,
  source: "connections_page",
  sync_interval_minutes: 60,
  health: "failed",
  collections: {
    capabilities: kind("failed", {
      last_run: {
        id: 9,
        kind: "capabilities",
        status: "failed",
        started_at: "2026-10-29T06:00:00Z",
        finished_at: "2026-10-29T06:00:01Z",
        seen: 0,
        error: "GitLab returned 401",
      },
    }),
    groups: kind("never"),
    projects: kind("never"),
  },
});

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, extra: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/connections": () => json({ connections: [primary, second] }),
    ...extra,
  });
}

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function body(call: Call | undefined): unknown {
  return JSON.parse(String(call?.init?.body ?? "null"));
}

afterEach(() => vi.unstubAllGlobals());

describe("ConnectionsPage", () => {
  it("shows each connection's health, source and last error", async () => {
    api(admin);
    renderAt("/connections");

    const eu = await screen.findByRole("region", { name: "EU GitLab" });
    expect(within(eu).getAllByText("Failed")).toHaveLength(2); // the badge and capabilities
    expect(within(eu).getByText("Added here")).toBeInTheDocument();
    expect(within(eu).getByText("Every 1 h")).toBeInTheDocument();
    expect(within(eu).getByText("GitLab returned 401")).toBeInTheDocument();
    const main = screen.getByRole("region", { name: "gitlab.example.test" });
    expect(within(main).getByText("Primary")).toBeInTheDocument();
    expect(within(main).getByText("Set in the deployment")).toBeInTheDocument();
    expect(within(main).queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });

  it("adds a connection with its token and never shows the token again", async () => {
    const added = connection(3, { name: "Lab", url: "https://lab.example.test", primary: false });
    const calls = api(admin, { "POST /api/v1/connections": () => json(added, 201) });
    renderAt("/connections");
    const form = await screen.findByRole("form", { name: "Add a GitLab connection" });

    await userEvent.type(within(form).getByLabelText("Name (optional)"), "Lab");
    await userEvent.type(within(form).getByLabelText("GitLab URL"), "https://lab.example.test");
    await userEvent.type(within(form).getByLabelText("Token (read_api)"), "glpat-secret");
    await userEvent.click(within(form).getByLabelText(/private network/));
    await userEvent.click(within(form).getByRole("button", { name: "Add connection" }));

    expect(await screen.findByText(/Lab added/)).toBeInTheDocument();
    const post = calls.find((call) => call.init?.method === "POST");
    expect(body(post)).toEqual({
      name: "Lab",
      url: "https://lab.example.test",
      token: "glpat-secret",
      timeout_seconds: 15,
      sync_interval_minutes: 360,
      allow_private_network: true,
    });
    expect(screen.queryByDisplayValue("glpat-secret")).not.toBeInTheDocument();
  });

  it("checks the form before sending", async () => {
    const calls = api(admin);
    renderAt("/connections");
    const form = await screen.findByRole("form", { name: "Add a GitLab connection" });

    await userEvent.type(within(form).getByLabelText("GitLab URL"), "http://plain.example.test");
    await userEvent.click(within(form).getByRole("button", { name: "Add connection" }));

    expect(await within(form).findByText(/starting https/)).toBeInTheDocument();
    expect(within(form).getByText(/read_api scope/)).toBeInTheDocument();
    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);
  });

  it("shows the API's refusal of a private host", async () => {
    api(admin, {
      "POST /api/v1/connections": () =>
        json({ detail: "eu.example.test resolves to the private address 10.0.0.5" }, 422),
    });
    renderAt("/connections");
    const form = await screen.findByRole("form", { name: "Add a GitLab connection" });

    await userEvent.type(within(form).getByLabelText("GitLab URL"), "https://eu.example.test");
    await userEvent.type(within(form).getByLabelText("Token (read_api)"), "glpat-x");
    await userEvent.click(within(form).getByRole("button", { name: "Add connection" }));

    expect(await within(form).findByText(/private address 10.0.0.5/)).toBeInTheDocument();
  });

  it("tests, pauses and removes an added connection", async () => {
    const calls = api(admin, {
      "POST /api/v1/connections/2/diagnostics": () =>
        json({
          status: "failed",
          checked_at: "2026-10-29T07:00:00Z",
          gitlab_url: "https://eu.example.test",
          version: null,
          checks: [{ id: "connectivity", status: "failed", summary: "The token was rejected" }],
        }),
      "PATCH /api/v1/connections/2": () => json({ ...second, paused: true }),
      "DELETE /api/v1/connections/2": () => new Response(null, { status: 204 }),
    });
    renderAt("/connections");
    const eu = await screen.findByRole("region", { name: "EU GitLab" });

    await userEvent.click(within(eu).getByRole("button", { name: "Test connection" }));
    expect(await within(eu).findByText("The token was rejected")).toBeInTheDocument();
    expect(within(eu).getByText("Test failed")).toBeInTheDocument();

    await userEvent.click(within(eu).getByRole("button", { name: "Pause" }));
    expect(await screen.findByText("EU GitLab paused.")).toBeInTheDocument();
    expect(body(calls.find((call) => call.init?.method === "PATCH"))).toEqual({ paused: true });

    await userEvent.click(
      within(await screen.findByRole("region", { name: "EU GitLab" })).getByRole("button", {
        name: "Remove",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Remove EU GitLab?" });
    expect(within(dialog).getByText(/evidence stay/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove connection" }));
    expect(await screen.findByText(/EU GitLab removed/)).toBeInTheDocument();
    expect(calls.some((call) => call.init?.method === "DELETE")).toBe(true);
  });

  it("collects a connection now", async () => {
    const calls = api(admin, {
      "POST /api/v1/connections/2/collect": () => json({ job_ids: [4, 5, 6] }, 202),
    });
    renderAt("/connections");
    const eu = await screen.findByRole("region", { name: "EU GitLab" });

    await userEvent.click(within(eu).getByRole("button", { name: "Collect now" }));

    expect(await screen.findByText("Collection of EU GitLab queued.")).toBeInTheDocument();
    expect(calls.some((call) => call.url.endsWith("/connections/2/collect"))).toBe(true);
  });
  it("adds a GitHub organization through an App installation", async () => {
    const added = connection(4, {
      provider: "github",
      url: "https://github.com/acme",
      primary: false,
      source: "connections_page",
      auth_kind: "app",
      app_id: 77,
      installation_id: 88,
      version: "github.com",
      edition: null,
    });
    const calls = api(admin, { "POST /api/v1/connections/github": () => json(added, 201) });
    renderAt("/connections");
    const form = await screen.findByRole("form", { name: "Add a GitHub organization" });

    await userEvent.click(within(form).getByRole("button", { name: "Add organization" }));
    expect(await within(form).findByText(/organization's name/)).toBeInTheDocument();
    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);

    await userEvent.type(within(form).getByLabelText("Organization on github.com"), "acme");
    await userEvent.selectOptions(within(form).getByLabelText("Sign in with"), "app");
    await userEvent.type(within(form).getByLabelText("App ID"), "77");
    await userEvent.type(within(form).getByLabelText("Installation ID"), "88");
    await userEvent.type(within(form).getByLabelText("Private key (PEM)"), "-----BEGIN KEY-----");
    await userEvent.click(within(form).getByRole("button", { name: "Add organization" }));

    expect(await screen.findByText(/github\.com\/acme added/)).toBeInTheDocument();
    const post = calls.find((call) => call.url.endsWith("/connections/github"));
    expect(body(post)).toEqual({
      name: "",
      organization: "acme",
      auth_kind: "app",
      secret: "-----BEGIN KEY-----",
      app_id: 77,
      installation_id: 88,
      timeout_seconds: 15,
      sync_interval_minutes: 360,
    });
  });

  it("shows a GitHub connection without GitLab's diagnostics", async () => {
    const org = connection(5, {
      provider: "github",
      url: "https://github.com/acme",
      primary: false,
      source: "connections_page",
      version: "github.com",
      edition: null,
    });
    api(admin, { "GET /api/v1/connections": () => json({ connections: [org] }) });
    renderAt("/connections");
    const card = await screen.findByRole("region", { name: "github.com/acme" });

    expect(within(card).getAllByText("GitHub").length).toBeGreaterThan(0);
    expect(within(card).queryByRole("button", { name: "Test connection" })).not.toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Collect now" })).toBeInTheDocument();
  });
});
