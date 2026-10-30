import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GitLabConfig, OidcConfig, SettingsInfo } from "../api/setup";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
  csrf_token: "a".repeat(64),
};

const gitlabConfig: GitLabConfig = {
  source: "database",
  url: "https://gitlab.example.test",
  token: "configured",
  timeout_seconds: 15,
  sync_interval_minutes: 360,
};

const oidcConfig: OidcConfig = {
  source: "database",
  public_url: "https://baluarte.example.test",
  issuer: "https://gitlab.example.test",
  client_id: "baluarte-client",
  client_secret: "configured",
  scopes: "openid profile email",
  roles_claim: "groups",
  admin_groups: ["platform/security"],
  analyst_groups: ["platform"],
  auditor_groups: [],
  redirect_uri: "https://baluarte.example.test/api/v1/auth/callback",
};

const info: SettingsInfo = {
  version: "0.1.0",
  logging: { level: "INFO", format: "json" },
  database: {
    host: "db",
    port: 5432,
    name: "baluarte",
    user: "baluarte",
    ssl_mode: null,
    password: "configured",
  },
  gitlab: gitlabConfig,
  authentication: {
    source: "database",
    public_url: oidcConfig.public_url,
    issuer: oidcConfig.issuer,
    client_id: oidcConfig.client_id,
    client_secret: "configured",
    scopes: oidcConfig.scopes,
    roles_claim: "groups",
    group_roles: { admin: ["platform/security"] },
    session_idle_minutes: 60,
    session_lifetime_minutes: 480,
  },
  secret_key: "configured",
};

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, routes: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/settings": () => json(info),
    "GET /api/v1/settings/gitlab": () => json(gitlabConfig),
    "GET /api/v1/settings/oidc": () => json(oidcConfig),
    ...routes,
  });
}

function renderSettings() {
  const router = createMemoryRouter(routes, { initialEntries: ["/settings"] });
  render(<RouterProvider router={router} />);
}

function card(name: string) {
  return screen.findByRole("region", { name });
}

function bodyOf(calls: Call[], method: string, url: string): unknown {
  const call = calls.filter((c) => c.url === url && c.init?.method === method).at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("settings page", () => {
  it("shows the configuration without any secret values", async () => {
    api(admin);

    renderSettings();

    const gitlab = await card("GitLab connection");
    expect(await within(gitlab).findByText("https://gitlab.example.test")).toBeInTheDocument();
    expect(within(gitlab).getByText("Configured (hidden)")).toBeInTheDocument();
    expect(within(gitlab).getByText("Every 6 hour(s)")).toBeInTheDocument();
    const oidc = await card("Identity provider");
    expect(await within(oidc).findByText("platform/security")).toBeInTheDocument();
    const system = await card("System");
    expect(await within(system).findByText("baluarte@db:5432/baluarte")).toBeInTheDocument();
    expect(within(system).getByText("0.1.0")).toBeInTheDocument();
  });

  it("is for administrators only and fetches nothing for others", async () => {
    const calls = api(analyst);

    renderSettings();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only administrators can view and change settings.",
    );
    expect(calls.some((call) => call.url.startsWith("/api/v1/settings"))).toBe(false);
  });

  it("edits the GitLab connection and keeps the stored token", async () => {
    const calls = api(admin, {
      "PUT /api/v1/settings/gitlab": () => json({ ...gitlabConfig, sync_interval_minutes: 0 }),
    });
    const user = userEvent.setup();

    renderSettings();
    const gitlab = await card("GitLab connection");
    await user.click(await within(gitlab).findByRole("button", { name: "Edit connection" }));
    expect(within(gitlab).getByLabelText("Access token")).toHaveValue("");
    const interval = within(gitlab).getByRole("spinbutton", { name: "Collect every (minutes)" });
    await user.clear(interval);
    await user.type(interval, "0");
    await user.click(within(gitlab).getByRole("button", { name: "Save GitLab connection" }));

    expect(await within(gitlab).findByText(/GitLab connection saved/)).toBeInTheDocument();
    expect(bodyOf(calls, "PUT", "/api/v1/settings/gitlab")).toEqual({
      url: "https://gitlab.example.test",
      token: null,
      timeout_seconds: 15,
      sync_interval_minutes: 0,
    });
    const put = calls.find((call) => call.init?.method === "PUT");
    expect((put?.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe(admin.csrf_token);
  });

  it("explains a validation refusal from the API", async () => {
    api(admin, {
      "PUT /api/v1/settings/gitlab": () =>
        json({ detail: [{ msg: "Value error, the GitLab URL must use https" }] }, 422),
    });
    const user = userEvent.setup();

    renderSettings();
    const gitlab = await card("GitLab connection");
    await user.click(await within(gitlab).findByRole("button", { name: "Edit connection" }));
    await user.click(within(gitlab).getByRole("button", { name: "Save GitLab connection" }));

    expect(await within(gitlab).findByRole("alert")).toHaveTextContent(
      "Refused: the GitLab URL must use https",
    );
  });

  it("disconnects GitLab after confirmation", async () => {
    let connected = true;
    const calls = api(admin, {
      "GET /api/v1/settings/gitlab": () =>
        connected ? json(gitlabConfig) : json({ detail: "Not found" }, 404),
      "DELETE /api/v1/settings/gitlab": () => {
        connected = false;
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();

    renderSettings();
    const gitlab = await card("GitLab connection");
    await user.click(await within(gitlab).findByRole("button", { name: "Disconnect" }));
    await user.click(within(gitlab).getByRole("button", { name: "Disconnect GitLab" }));

    expect(await within(gitlab).findByText(/GitLab is not connected/)).toBeInTheDocument();
    expect(within(gitlab).getByText(/GitLab was disconnected/)).toBeInTheDocument();
    expect(within(gitlab).getByRole("button", { name: "Connect GitLab" })).toBeInTheDocument();
    const deleted = calls.find((call) => call.init?.method === "DELETE");
    expect((deleted?.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe(
      admin.csrf_token,
    );
  });

  it("shows deployment-managed sections as read-only", async () => {
    api(admin, {
      "GET /api/v1/settings/gitlab": () => json({ ...gitlabConfig, source: "deployment" }),
      "GET /api/v1/settings/oidc": () => json({ ...oidcConfig, source: "deployment" }),
    });

    renderSettings();

    const gitlab = await card("GitLab connection");
    expect(await within(gitlab).findByText(/managed by the deployment/)).toBeInTheDocument();
    expect(within(gitlab).queryByRole("button")).not.toBeInTheDocument();
    const oidc = await card("Identity provider");
    expect(await within(oidc).findByText(/managed by the deployment/)).toBeInTheDocument();
    expect(within(oidc).queryByRole("button")).not.toBeInTheDocument();
  });

  it("warns about lockout when editing the identity provider", async () => {
    const calls = api(admin, { "PUT /api/v1/settings/oidc": () => json(oidcConfig) });
    const user = userEvent.setup();

    renderSettings();
    const oidc = await card("Identity provider");
    await user.click(await within(oidc).findByRole("button", { name: "Edit identity provider" }));

    expect(within(oidc).getByText(/can lock every administrator out/)).toBeInTheDocument();
    await user.click(within(oidc).getByRole("button", { name: "Save identity provider" }));
    expect(await within(oidc).findByText(/Identity provider saved/)).toBeInTheDocument();
    expect(bodyOf(calls, "PUT", "/api/v1/settings/oidc")).toMatchObject({
      client_secret: null,
      admin_groups: ["platform/security"],
      analyst_groups: ["platform"],
    });
  });
});
