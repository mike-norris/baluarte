import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routes } from "../routes";
import { signInErrors } from "../pages/SignInPage";
import { analyst, json, mockApi, signedIn } from "../test/api";
import { signInHref } from "./session";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("session gate", () => {
  it("asks anonymous users to sign in and returns them to the same page", async () => {
    mockApi({ "GET /api/v1/auth/me": () => json({ detail: "Not authenticated" }, 401) });

    renderAt("/findings?state=open");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in to Baluarte" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/api/v1/auth/login?return_to=%2Ffindings%3Fstate%3Dopen",
    );
    expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
  });

  it("explains when sign-in is not configured", async () => {
    mockApi({ "GET /api/v1/auth/me": () => json({ detail: "not configured" }, 503) });

    renderAt("/");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign-in is not configured" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Sign in" })).not.toBeInTheDocument();
  });

  it("shows progress while the session is checked", () => {
    vi.stubGlobal("fetch", () => new Promise(() => undefined));

    renderAt("/");

    expect(screen.getByRole("status")).toHaveTextContent("Checking your session");
  });

  it("offers a retry when the API is unreachable", async () => {
    let attempts = 0;
    mockApi({
      "GET /api/v1/auth/me": () => {
        attempts += 1;
        if (attempts === 1) throw new TypeError("Failed to fetch");
        return json(analyst);
      },
    });
    const user = userEvent.setup();

    renderAt("/inventory");
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Inventory" }),
    ).toBeInTheDocument();
  });

  it("sends the session cookie when checking the session", async () => {
    const calls = signedIn();

    renderAt("/");
    await screen.findByRole("navigation", { name: "Primary" });

    expect(calls[0]?.init?.credentials).toBe("same-origin");
  });
});

describe("account menu", () => {
  it("shows who is signed in and their roles", async () => {
    signedIn();

    renderAt("/");

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("(analyst)")).toBeInTheDocument();
  });

  it("falls back to email, then subject, for the name", async () => {
    signedIn({ ...analyst, display_name: null, email: null });

    renderAt("/");

    expect(await screen.findByText("user-1")).toBeInTheDocument();
  });

  it("signs out with the CSRF token and returns to the sign-in prompt", async () => {
    const calls = mockApi({
      "GET /api/v1/auth/me": () => json(analyst),
      "POST /api/v1/auth/logout": () => new Response(null, { status: 204 }),
    });
    const user = userEvent.setup();

    renderAt("/");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign in to Baluarte" }),
    ).toBeInTheDocument();
    const logout = calls.find((call) => call.url === "/api/v1/auth/logout");
    expect(logout?.init?.credentials).toBe("same-origin");
    expect(logout?.init?.headers).toEqual({ "X-CSRF-Token": analyst.csrf_token });
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("reports a failed sign-out and stays signed in", async () => {
    mockApi({
      "GET /api/v1/auth/me": () => json(analyst),
      "POST /api/v1/auth/logout": () => json({ detail: "Missing or invalid CSRF token" }, 403),
    });
    const user = userEvent.setup();

    // A section that makes no API calls of its own, so the only alert is the sign-out error.
    renderAt("/policies");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-out failed");
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });
});

describe("sign-in page", () => {
  it.each(Object.entries(signInErrors))("explains the %s error", async (code, message) => {
    mockApi({ "GET /api/v1/auth/me": () => json({}, 401) });

    renderAt(`/sign-in?error=${code}`);

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/api/v1/auth/login?return_to=%2F",
    );
  });

  it("uses a generic message for unknown codes and never echoes them", async () => {
    mockApi({ "GET /api/v1/auth/me": () => json({}, 401) });

    renderAt("/sign-in?error=%3Cscript%3Ealert(1)%3C%2Fscript%3E");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(signInErrors.sign_in_failed ?? "");
    expect(alert).not.toHaveTextContent("script");
  });

  it("sends signed-in users to the dashboard", async () => {
    signedIn();

    const router = renderAt("/sign-in");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Dashboard" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });
});

describe("signInHref", () => {
  it("never returns to the sign-in page itself", () => {
    expect(signInHref("/sign-in?error=expired")).toBe("/api/v1/auth/login?return_to=%2F");
  });
});
