import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GitLabConfig, OidcConfig, SettingsInfo } from "../api/setup";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { controlException, profile, shippingConfig, shippingStatus } from "../test/compliance";
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

const socProfile = profile({
  frameworks: ["soc2"],
  configured: true,
  settings: { ...profile().settings, session_idle_minutes: 30, require_review: true },
  sources: { ...profile().sources, session_lifetime_minutes: "deployment" },
  updated_by: "user:1",
  updated_at: "2026-11-02T09:00:00Z",
});

function api(user: CurrentUser, routes: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/settings": () => json(info),
    "GET /api/v1/settings/gitlab": () => json(gitlabConfig),
    "GET /api/v1/settings/oidc": () => json(oidcConfig),
    "GET /api/v1/settings/governance": () => json({ require_review: false }),
    "GET /api/v1/compliance/profile": () => json(socProfile),
    "GET /api/v1/settings/audit-shipping": () => json(shippingConfig()),
    "GET /api/v1/compliance/audit-shipping": () => json(shippingStatus()),
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

  it("turns on two-person review of baseline changes", async () => {
    let required = false;
    const calls = api(admin, {
      "GET /api/v1/settings/governance": () => json({ require_review: required }),
      "PUT /api/v1/settings/governance": (init) => {
        required = (JSON.parse(String(init?.body)) as { require_review: boolean }).require_review;
        return json({ require_review: required });
      },
    });
    const user = userEvent.setup();
    render(
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/settings"] })} />,
    );

    const card = await screen.findByRole("region", { name: "Baseline change review" });
    const toggle = await within(card).findByRole("checkbox", {
      name: "Require a second person to approve baseline changes",
    });
    expect(toggle).not.toBeChecked();

    await user.click(toggle);

    expect(
      await within(card).findByText(
        "Baseline changes now wait for a second person to approve them.",
      ),
    ).toBeInTheDocument();
    expect(await within(card).findByRole("checkbox")).toBeChecked();
    const put = calls.find((call) => call.init?.method === "PUT");
    expect(put?.url).toBe("/api/v1/settings/governance");
    expect((put?.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe(admin.csrf_token);
  });
});

describe("compliance profile in settings", () => {
  it("shows the frameworks and settings, marking what the deployment sets", async () => {
    api(admin);
    renderSettings();

    const section = await card("Compliance profile");
    expect(await within(section).findByText("SOC 2 Type II")).toBeInTheDocument();
    expect(within(section).getByText("30 minutes")).toBeInTheDocument();
    expect(within(section).getByText("480 minutes (set by the deployment)")).toBeInTheDocument();
    expect(
      within(section).getByRole("link", { name: "See the compliance posture" }),
    ).toHaveAttribute("href", "/compliance");
  });

  it("asks for a profile when none has been chosen", async () => {
    api(admin, { "GET /api/v1/compliance/profile": () => json(profile()) });
    renderSettings();

    const section = await card("Compliance profile");
    expect(await within(section).findByText(/No compliance profile has been chosen/)).toBeVisible();
    expect(within(section).getByRole("button", { name: "Choose a profile" })).toBeInTheDocument();
  });

  it("offers an exception for each value the API refuses as below the minimum", async () => {
    const calls = api(admin, {
      "PUT /api/v1/compliance/profile": () =>
        json(
          {
            detail: {
              code: "below_minimum",
              violations: [
                {
                  control_id: "session-idle",
                  setting: "session_idle_minutes",
                  value: 45,
                  required: { op: "<=", value: 30 },
                  frameworks: ["soc2"],
                },
              ],
            },
          },
          409,
        ),
      "POST /api/v1/compliance/exceptions": () =>
        json(controlException({ control_id: "session-idle" }), 201),
    });
    const user = userEvent.setup();
    renderSettings();

    const section = await card("Compliance profile");
    await user.click(await within(section).findByRole("button", { name: "Edit profile" }));
    expect(within(section).getByRole("checkbox", { name: "SOC 2 Type II" })).toBeChecked();
    expect(
      within(section).getByRole("textbox", { name: "Sign out after at most (minutes)" }),
    ).toBeDisabled();
    const idle = within(section).getByRole("textbox", {
      name: "Sign out after inactivity of (minutes)",
    });
    await user.clear(idle);
    await user.type(idle, "45");
    expect(idle).toHaveAccessibleDescription(/saving needs an exception in force/);
    await user.click(within(section).getByRole("button", { name: "Save" }));

    const refusal = await within(section).findByText(/Not saved: these values do not meet/);
    expect(refusal.parentElement).toHaveTextContent(
      "Sign out after inactivity of: 45. At most 30 minutes (SOC 2).",
    );
    expect(bodyOf(calls, "PUT", "/api/v1/compliance/profile")).toMatchObject({
      frameworks: ["soc2"],
      settings: { session_idle_minutes: 45, require_review: true },
    });

    await user.click(within(section).getByRole("button", { name: /Request an exception/ }));
    const form = within(section).getByRole("form", { name: "Request an exception" });
    expect(within(form).getByRole("combobox", { name: "Control" })).toBeDisabled();
    await user.type(
      within(form).getByRole("textbox", { name: "Justification" }),
      "Operators work long investigations; reviewed monthly.",
    );
    await user.click(within(form).getByRole("button", { name: "Request exception" }));

    expect(await within(section).findByText(/waiting for another administrator/)).toBeVisible();
    expect(bodyOf(calls, "POST", "/api/v1/compliance/exceptions")).toMatchObject({
      control_id: "session-idle",
      justification: "Operators work long investigations; reviewed monthly.",
    });
  });

  it("warns what legal hold does before it is saved", async () => {
    api(admin);
    const user = userEvent.setup();
    renderSettings();

    const section = await card("Compliance profile");
    await user.click(await within(section).findByRole("button", { name: "Edit profile" }));
    await user.click(within(section).getByRole("checkbox", { name: "Legal hold: delete nothing" }));

    expect(within(section).getByText(/the retention job deletes nothing/)).toBeVisible();
  });
});

describe("audit log shipping in settings", () => {
  it("shows the destination and how far delivery has got, without secrets", async () => {
    api(admin, {
      "GET /api/v1/settings/audit-shipping": () =>
        json(
          shippingConfig({
            enabled: true,
            type: "elasticsearch",
            elasticsearch: {
              url: "https://logs.example.test:9200",
              index: "baluarte-audit",
              auth: "api_key",
              username: null,
              ca_pem: null,
            },
            api_key: "configured",
          }),
        ),
      "GET /api/v1/compliance/audit-shipping": () =>
        json(
          shippingStatus({
            enabled: true,
            type: "elasticsearch",
            destination: "https://logs.example.test:9200 (baluarte-audit)",
            last_shipped_id: 40,
            lag_events: 2,
            oldest_unshipped_at: "2026-11-02T11:59:00Z",
            last_success_at: "2026-11-02T11:58:00Z",
            last_error: "connection refused",
            consecutive_failures: 3,
          }),
        ),
    });
    renderSettings();

    const section = await card("Audit log shipping");
    expect(await within(section).findByText("https://logs.example.test:9200")).toBeVisible();
    expect(section).toHaveTextContent("API key: Configured (hidden)");
    const status = within(section).getByRole("group", { name: "Shipping status" });
    expect(status).toHaveTextContent("2 events waiting");
    expect(status).toHaveTextContent("(up to event 40)");
    expect(status).toHaveTextContent("connection refused (3 failed attempts in a row)");
  });

  it("checks the destination, sends a test message and saves", async () => {
    const calls = api(admin, {
      "POST /api/v1/settings/audit-shipping/test": () =>
        json({ ok: true, detail: "Delivered to logs.example.test:6514." }),
      "PUT /api/v1/settings/audit-shipping": () =>
        json(
          shippingConfig({
            enabled: true,
            syslog: { host: "logs.example.test", port: 6514, server_name: null, ca_pem: null },
          }),
        ),
    });
    const user = userEvent.setup();
    renderSettings();

    const section = await card("Audit log shipping");
    await user.click(
      await within(section).findByRole("button", { name: "Edit audit log shipping" }),
    );
    await user.click(
      within(section).getByRole("checkbox", { name: "Ship audit events to this destination" }),
    );
    await user.click(within(section).getByRole("button", { name: "Save" }));
    expect(within(section).getByRole("alert")).toHaveTextContent(
      "Syslog host: enter the collector's host name.",
    );

    await user.type(
      within(section).getByRole("textbox", { name: "Syslog host" }),
      "logs.example.test",
    );
    await user.click(within(section).getByRole("button", { name: "Send test message" }));
    expect(await within(section).findByText(/Test message delivered/)).toBeVisible();

    await user.click(within(section).getByRole("button", { name: "Save" }));
    expect(await within(section).findByText(/Audit log shipping saved/)).toBeVisible();
    const expected = {
      enabled: true,
      type: "syslog_tls",
      syslog: { host: "logs.example.test", port: 6514, server_name: null, ca_pem: null },
      elasticsearch: {
        url: "",
        index: "baluarte-audit",
        auth: "api_key",
        username: null,
        ca_pem: null,
      },
      api_key: null,
      password: null,
    };
    expect(bodyOf(calls, "POST", "/api/v1/settings/audit-shipping/test")).toEqual(expected);
    expect(bodyOf(calls, "PUT", "/api/v1/settings/audit-shipping")).toEqual(expected);
  });

  it("refuses plain http and asks for the key again when the address changes", async () => {
    const calls = api(admin, {
      "GET /api/v1/settings/audit-shipping": () =>
        json(
          shippingConfig({
            enabled: true,
            type: "elasticsearch",
            elasticsearch: {
              url: "https://logs.example.test:9200",
              index: "baluarte-audit",
              auth: "api_key",
              username: null,
              ca_pem: null,
            },
            api_key: "configured",
          }),
        ),
      "PUT /api/v1/settings/audit-shipping": () => json(shippingConfig()),
    });
    const user = userEvent.setup();
    renderSettings();

    const section = await card("Audit log shipping");
    await user.click(
      await within(section).findByRole("button", { name: "Edit audit log shipping" }),
    );
    const address = within(section).getByRole("textbox", { name: "Address" });
    await user.clear(address);
    await user.type(address, "http://logs.example.test:9200");
    await user.click(within(section).getByRole("button", { name: "Save" }));
    const alert = within(section).getByRole("alert");
    expect(alert).toHaveTextContent("enter an https:// address");
    expect(alert).toHaveTextContent("API key: enter it again, because the address changed.");

    await user.clear(address);
    await user.type(address, "https://other.example.test:9200");
    await user.type(
      within(section).getByLabelText("API key", { selector: "input[type=password]" }),
      "new-key-value",
    );
    await user.click(within(section).getByRole("button", { name: "Save" }));
    expect(await within(section).findByText(/Audit log shipping saved/)).toBeVisible();
    expect(bodyOf(calls, "PUT", "/api/v1/settings/audit-shipping")).toMatchObject({
      type: "elasticsearch",
      elasticsearch: { url: "https://other.example.test:9200" },
      api_key: "new-key-value",
      password: null,
    });
  });
});
