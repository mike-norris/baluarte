import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GitLabConfig, GitLabTestResult, OidcConfig, SetupStatus } from "../api/setup";
import { routes } from "../routes";
import { json, mockApi, type Call } from "../test/api";

const ME = "GET /api/v1/auth/me";
const STATUS = "GET /api/v1/setup/status";
const SESSION = "POST /api/v1/setup/session";
const OIDC = "GET /api/v1/setup/oidc";
const GITLAB = "GET /api/v1/setup/gitlab";
const CSRF = "s".repeat(64);

function status(extra: Partial<SetupStatus> = {}): SetupStatus {
  return {
    state: "needs_setup",
    setup_session: false,
    secret_key_configured: true,
    oidc: { source: null, configured: false },
    gitlab: { source: null, configured: false },
    ...extra,
  };
}

const oidcConfig: OidcConfig = {
  source: "database",
  public_url: "http://localhost:3000",
  issuer: "https://gitlab.example.test",
  client_id: "baluarte-client",
  client_secret: "configured",
  scopes: "openid profile email",
  roles_claim: "groups",
  admin_groups: ["platform/security"],
  analyst_groups: [],
  auditor_groups: ["audit"],
  redirect_uri: "http://localhost:3000/api/v1/auth/callback",
};

const gitlabConfig: GitLabConfig = {
  source: "database",
  url: "https://gitlab.example.test",
  token: "configured",
  timeout_seconds: 15,
  sync_interval_minutes: 360,
};

const notFound = () => json({ detail: "Not found" }, 404);

type Routes = Record<string, (init?: RequestInit) => Response>;

/** Signed out, with a setup status that tests can change between calls. */
function api(current: () => SetupStatus, routes: Routes = {}): Call[] {
  return mockApi({
    [ME]: () => json({ detail: "Authentication is not configured" }, 503),
    [STATUS]: () => json(current()),
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
  const call = calls.filter((c) => c.url === url && (c.init?.method ?? "GET") === method).at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

function headersOf(calls: Call[], url: string): Record<string, string> | undefined {
  return calls.filter((c) => c.url === url).at(-1)?.init?.headers as
    Record<string, string> | undefined;
}

/** Opens the wizard with a setup session already held. */
function inSession(extra: Partial<SetupStatus> = {}, routes: Routes = {}): Call[] {
  window.sessionStorage.setItem("baluarte.setup.csrf", CSRF);
  return api(() => status({ setup_session: true, ...extra }), routes);
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("routing to setup", () => {
  it("sends visitors to the setup wizard while Baluarte is not installed", async () => {
    api(() => status());

    const router = renderAt("/findings");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Set up Baluarte" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/setup");
    expect(await screen.findByRole("textbox", { name: "Setup code" })).toBeInTheDocument();
  });

  it("keeps the sign-in prompt when the server has no setup (older API)", async () => {
    mockApi({
      [ME]: () => json({ detail: "Not authenticated" }, 401),
      [STATUS]: notFound,
    });

    const router = renderAt("/");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in to Baluarte" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("says setup is complete and offers sign-in", async () => {
    api(() => ({ ...status({ state: "complete" }), oidc: null, gitlab: null }));

    renderAt("/setup");

    expect(await screen.findByText("Setup is complete.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/api/v1/auth/login?return_to=%2F",
    );
    expect(screen.queryByRole("textbox", { name: "Setup code" })).not.toBeInTheDocument();
  });
});

describe("setup code", () => {
  it.each([
    [401, "That setup code is not valid."],
    [429, "Too many wrong codes. Wait 15 minutes and try again."],
  ])("explains a %i response", async (code, message) => {
    api(() => status(), { [SESSION]: () => json({ detail: "no" }, code) });
    const user = userEvent.setup();

    renderAt("/setup");
    await user.type(await screen.findByRole("textbox", { name: "Setup code" }), "AAAA-BBBB");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).toHaveFocus();
  });

  it("asks for the code before sending anything", async () => {
    const calls = api(() => status());
    const user = userEvent.setup();

    renderAt("/setup");
    await user.click(await screen.findByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the setup code.");
    expect(calls.some((call) => call.url === "/api/v1/setup/session")).toBe(false);
  });

  it("opens the wizard with the code, and keeps its CSRF token for the tab", async () => {
    let session = false;
    const calls = api(() => status({ setup_session: session }), {
      [SESSION]: () => {
        session = true;
        return json({ csrf_token: CSRF });
      },
      [OIDC]: notFound,
    });
    const user = userEvent.setup();

    renderAt("/setup");
    await user.type(await screen.findByRole("textbox", { name: "Setup code" }), " AAAA-BBBB ");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(
      await screen.findByRole("heading", { name: "Step 1 of 3: Identity provider" }),
    ).toBeInTheDocument();
    expect(bodyOf(calls, SESSION)).toEqual({ code: "AAAA-BBBB" });
    expect(window.sessionStorage.getItem("baluarte.setup.csrf")).toBe(CSRF);
  });

  it("offers sign-in when only the admin sign-in is left", async () => {
    api(() =>
      status({ state: "awaiting_admin_sign_in", oidc: { source: "database", configured: true } }),
    );

    renderAt("/setup");

    expect(await screen.findByRole("link", { name: "Sign in to finish setup" })).toHaveAttribute(
      "href",
      "/api/v1/auth/login?return_to=%2Fsettings",
    );
    expect(screen.getByRole("textbox", { name: "Setup code" })).toBeInTheDocument();
  });
});

describe("identity provider step", () => {
  it("prefills the address and shows the redirect URI to register", async () => {
    inSession({}, { [OIDC]: notFound });

    renderAt("/setup");

    const address = await screen.findByRole("textbox", { name: "Baluarte address" });
    expect(address).toHaveValue(window.location.origin);
    expect(screen.getByText(`${window.location.origin}/api/v1/auth/callback`)).toBeInTheDocument();
  });

  it("lists every problem before sending", async () => {
    const calls = inSession({}, { [OIDC]: notFound });
    const user = userEvent.setup();

    renderAt("/setup");
    await user.click(await screen.findByRole("button", { name: "Save and continue" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveFocus();
    expect(alert).toHaveTextContent("Enter the issuer as an https URL.");
    expect(alert).toHaveTextContent("Enter the client ID");
    expect(alert).toHaveTextContent("Map at least one group to the admin role");
    expect(calls.some((call) => call.init?.method === "PUT")).toBe(false);
  });

  it("tests discovery with the setup session's CSRF token", async () => {
    const calls = inSession(
      {},
      {
        [OIDC]: notFound,
        "POST /api/v1/setup/oidc/test": () =>
          json({
            ok: true,
            issuer: "https://gitlab.example.test",
            authorization_endpoint: "https://gitlab.example.test/oauth/authorize",
            scopes_supported: ["openid", "profile", "email", "read_api"],
            error: null,
          }),
      },
    );
    const user = userEvent.setup();

    renderAt("/setup");
    await user.type(
      await screen.findByRole("textbox", { name: "Issuer" }),
      "https://gitlab.example.test",
    );
    await user.click(screen.getByRole("button", { name: "Test discovery" }));

    expect(await screen.findByText("Discovery succeeded")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/setup/oidc/test")).toEqual({
      issuer: "https://gitlab.example.test",
    });
    expect(headersOf(calls, "/api/v1/setup/oidc/test")?.["X-CSRF-Token"]).toBe(CSRF);
  });

  it("shows a failed discovery", async () => {
    inSession(
      {},
      {
        [OIDC]: notFound,
        "POST /api/v1/setup/oidc/test": () =>
          json({
            ok: false,
            issuer: null,
            authorization_endpoint: null,
            scopes_supported: [],
            error: "The discovery document could not be fetched (HTTP 404).",
          }),
      },
    );
    const user = userEvent.setup();

    renderAt("/setup");
    await user.type(await screen.findByRole("textbox", { name: "Issuer" }), "https://nope.test");
    await user.click(screen.getByRole("button", { name: "Test discovery" }));

    expect(await screen.findByText("Discovery failed")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 404/)).toBeInTheDocument();
  });

  it("saves the provider and moves to the GitLab step", async () => {
    const calls = inSession(
      {},
      {
        [OIDC]: notFound,
        [GITLAB]: notFound,
        "PUT /api/v1/setup/oidc": () => json(oidcConfig),
      },
    );
    const user = userEvent.setup();

    renderAt("/setup");
    await user.type(
      await screen.findByRole("textbox", { name: "Issuer" }),
      "https://gitlab.example.test",
    );
    await user.type(screen.getByRole("textbox", { name: "Client ID" }), "baluarte-client");
    await user.type(screen.getByLabelText("Client secret"), "s3cret");
    await user.type(
      screen.getByRole("textbox", { name: "Admin groups" }),
      "platform/security\nplatform/security, ops",
    );
    await user.click(screen.getByRole("button", { name: "Save and continue" }));

    const heading = await screen.findByRole("heading", { name: "Step 2 of 3: GitLab" });
    expect(heading).toHaveFocus();
    expect(bodyOf(calls, "PUT /api/v1/setup/oidc")).toEqual({
      public_url: window.location.origin,
      issuer: "https://gitlab.example.test",
      client_id: "baluarte-client",
      client_secret: "s3cret",
      clear_client_secret: false,
      scopes: "openid profile email",
      roles_claim: "groups",
      admin_groups: ["platform/security", "ops"],
      analyst_groups: [],
      auditor_groups: [],
    });
    expect(headersOf(calls, "/api/v1/setup/oidc")?.["X-CSRF-Token"]).toBe(CSRF);
  });

  it("keeps a stored secret unless a new one is entered", async () => {
    const calls = inSession(
      { oidc: { source: "database", configured: true } },
      {
        [OIDC]: () => json(oidcConfig),
        [GITLAB]: notFound,
        "PUT /api/v1/setup/oidc": () => json(oidcConfig),
      },
    );
    const user = userEvent.setup();

    renderAt("/setup");
    expect(await screen.findByRole("textbox", { name: "Client ID" })).toHaveValue(
      "baluarte-client",
    );
    expect(screen.getByText(/A secret is stored and is never shown/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save and continue" }));

    await screen.findByRole("heading", { name: "Step 2 of 3: GitLab" });
    expect(bodyOf(calls, "PUT /api/v1/setup/oidc")).toMatchObject({
      client_secret: null,
      clear_client_secret: false,
      admin_groups: ["platform/security"],
      auditor_groups: ["audit"],
    });
  });

  it("explains a refusal from the API", async () => {
    inSession(
      {},
      {
        [OIDC]: () => json(oidcConfig),
        "PUT /api/v1/setup/oidc": () =>
          json({ detail: "BALUARTE_SECRET_KEY_FILE is not configured" }, 409),
      },
    );
    const user = userEvent.setup();

    renderAt("/setup");
    await user.click(await screen.findByRole("button", { name: "Save and continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Not possible now: BALUARTE_SECRET_KEY_FILE is not configured",
    );
  });

  it("shows deployment-managed sign-in as read-only", async () => {
    inSession(
      { oidc: { source: "deployment", configured: true } },
      { [OIDC]: () => json({ ...oidcConfig, source: "deployment" }) },
    );

    renderAt("/setup");

    expect(await screen.findByText(/Sign-in is managed by the deployment/)).toBeInTheDocument();
    expect(screen.getByText("baluarte-client")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Client ID" })).not.toBeInTheDocument();
  });

  it("explains the missing encryption key instead of asking for secrets", async () => {
    inSession({ secret_key_configured: false }, { [OIDC]: notFound });

    renderAt("/setup");

    await screen.findByRole("textbox", { name: "Client ID" });
    expect(screen.getAllByText("Secrets cannot be saved yet").length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Client secret")).not.toBeInTheDocument();
  });

  it("asks for the code again when the setup session ends", async () => {
    let session = true;
    window.sessionStorage.setItem("baluarte.setup.csrf", CSRF);
    api(() => status({ setup_session: session }), {
      [OIDC]: () => {
        session = false;
        return json({ detail: "Not authenticated" }, 401);
      },
    });

    renderAt("/setup");

    expect(await screen.findByText(/Your setup session ended/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Setup code" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("baluarte.setup.csrf")).toBeNull();
  });
});

describe("GitLab step", () => {
  const adminToken: GitLabTestResult = {
    ok: true,
    version: "18.5.1-ee",
    user: { username: "root", is_admin: true },
    token_scopes: ["api", "read_api"],
    token_expires_at: null,
    checks: [
      { name: "Token scopes", status: "warn", message: "The token has more than read_api." },
      { name: "Reachability", status: "pass", message: "GitLab answered in 120 ms." },
    ],
    error: null,
  };

  async function openGitLabStep(routes: Routes, extra: Partial<SetupStatus> = {}) {
    const calls = inSession(
      { oidc: { source: "database", configured: true }, ...extra },
      { [OIDC]: () => json(oidcConfig), ...routes },
    );
    const user = userEvent.setup();
    renderAt("/setup");
    await user.click(await screen.findByRole("button", { name: "Continue without changes" }));
    await screen.findByRole("heading", { name: "Step 2 of 3: GitLab" });
    return { calls, user };
  }

  it("tests the connection and warns about an administrator token", async () => {
    const { calls, user } = await openGitLabStep({
      [GITLAB]: notFound,
      "POST /api/v1/setup/gitlab/test": () => json(adminToken),
    });

    await user.type(
      await screen.findByRole("textbox", { name: "GitLab address" }),
      "https://gitlab.example.test",
    );
    await user.type(screen.getByLabelText("Access token"), "glpat-secret");
    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("Connection succeeded")).toBeInTheDocument();
    expect(screen.getByText("GitLab version: 18.5.1-ee")).toBeInTheDocument();
    expect(screen.getByText(/belongs to a GitLab administrator/)).toBeInTheDocument();
    expect(screen.getByText("The token has more than read_api.")).toBeInTheDocument();
    expect(screen.getByText("Token expires: never (no expiry set)")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/setup/gitlab/test")).toEqual({
      url: "https://gitlab.example.test",
      token: "glpat-secret",
    });
  });

  it("requires a token before saving a new connection", async () => {
    const { calls, user } = await openGitLabStep({ [GITLAB]: notFound });

    await user.type(
      await screen.findByRole("textbox", { name: "GitLab address" }),
      "https://gitlab.example.test/api/v4?x=1",
    );
    await user.click(screen.getByRole("button", { name: "Save and continue" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Enter the GitLab address as an https URL");
    expect(alert).toHaveTextContent("Enter a token with the read_api scope.");
    expect(calls.some((call) => call.url === "/api/v1/setup/gitlab")).toBe(true);
    expect(calls.some((call) => call.init?.method === "PUT")).toBe(false);
  });

  it("saves the connection and shows the review with sign-in to finish", async () => {
    let saved = false;
    const { calls, user } = await openGitLabStep({
      [GITLAB]: () => (saved ? json(gitlabConfig) : notFound()),
      "PUT /api/v1/setup/gitlab": () => {
        saved = true;
        return json(gitlabConfig);
      },
    });

    await user.type(
      await screen.findByRole("textbox", { name: "GitLab address" }),
      "https://gitlab.example.test/",
    );
    await user.type(screen.getByLabelText("Access token"), "glpat-secret");
    const interval = screen.getByRole("spinbutton", { name: "Collect every (minutes)" });
    await user.clear(interval);
    await user.type(interval, "60");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));

    expect(
      await screen.findByRole("heading", { name: "Step 3 of 3: Review and finish" }),
    ).toBeInTheDocument();
    expect(bodyOf(calls, "PUT /api/v1/setup/gitlab")).toEqual({
      url: "https://gitlab.example.test",
      token: "glpat-secret",
      timeout_seconds: 15,
      sync_interval_minutes: 60,
    });
    const review = await screen.findByRole("region", { name: "GitLab" });
    expect(within(review).getByText("Configured (hidden)")).toBeInTheDocument();
    expect(within(review).getByText("Every 6 hour(s)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in to finish setup" })).toHaveAttribute(
      "href",
      "/api/v1/auth/login?return_to=%2Fsettings",
    );
    expect(document.body).not.toHaveTextContent("glpat-secret");
  });

  it("can be skipped", async () => {
    const { user } = await openGitLabStep({ [GITLAB]: notFound });

    await user.click(await screen.findByRole("button", { name: "Skip for now" }));

    expect(
      await screen.findByRole("heading", { name: "Step 3 of 3: Review and finish" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Not connected/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in to finish setup" })).toBeInTheDocument();
  });

  it("shows a deployment-managed connection as read-only", async () => {
    await openGitLabStep(
      { [GITLAB]: () => json({ ...gitlabConfig, source: "deployment" }) },
      { gitlab: { source: "deployment", configured: true } },
    );

    expect(
      await screen.findByText(/The GitLab connection is managed by the deployment/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "GitLab address" })).not.toBeInTheDocument();
  });
});
