import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MfaStatus, MfaVerified } from "../api/mfa";
import { routes } from "../routes";
import { json, mockApi, type Call } from "../test/api";

const CSRF = "m".repeat(64);
const STATUS = "GET /api/v1/auth/mfa";
const ENROLL = "POST /api/v1/auth/mfa/enroll";
const VERIFY = "POST /api/v1/auth/mfa/verify";

function status(extra: Partial<MfaStatus> = {}): MfaStatus {
  return {
    enrolled: false,
    account: "ada@example.test",
    csrf_token: CSRF,
    max_failures: 5,
    failures: 0,
    ...extra,
  };
}

function verified(extra: Partial<MfaVerified> = {}): MfaVerified {
  return {
    return_to: "/findings",
    method: "totp",
    recovery_codes: [],
    recovery_codes_left: null,
    ...extra,
  };
}

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(routes: Routes): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json({ detail: "Not authenticated" }, 401),
    ...routes,
  });
}

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function bodyOf(calls: Call[], method: string, url: string): unknown {
  const call = calls.filter((c) => c.url === url && c.init?.method === method).at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

const assign = vi.fn();

beforeEach(() => {
  assign.mockReset();
  vi.stubGlobal("location", { ...window.location, assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the authenticator step of sign-in", () => {
  it("sets up an authenticator, then shows the recovery codes once", async () => {
    const codes = ["abcde-fghjk", "mnpqr-stuvw"];
    const calls = api({
      [STATUS]: () => json(status()),
      [ENROLL]: () =>
        json({ secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://totp/x", qr: ["101", "010", "101"] }),
      [VERIFY]: () => json(verified({ recovery_codes: codes })),
    });
    const user = userEvent.setup();

    renderAt("/mfa?return_to=%2Ffindings");
    await user.click(await screen.findByRole("button", { name: "Set up an authenticator app" }));
    expect(await screen.findByRole("img", { name: /QR code to add Baluarte/ })).toBeInTheDocument();
    expect(screen.getByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Code from the app" }), "123456");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("abcde-fghjk")).toBeInTheDocument();
    expect(bodyOf(calls, "POST", "/api/v1/auth/mfa/verify")).toEqual({
      code: "123456",
      return_to: "/findings",
    });
    const headers = calls.find((c) => c.url === "/api/v1/auth/mfa/verify")?.init?.headers as
      Record<string, string> | undefined;
    expect(headers?.["X-CSRF-Token"]).toBe(CSRF);
    expect(assign).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "I have saved these codes" }));
    expect(assign).toHaveBeenCalledWith("/findings");
  });

  it("asks an enrolled user for a code and shows why a code was refused", async () => {
    let attempts = 0;
    api({
      [STATUS]: () => json(status({ enrolled: true })),
      [VERIFY]: () => {
        attempts += 1;
        return attempts === 1
          ? json({ detail: "That code is not valid. 4 tries left." }, 400)
          : json(verified());
      },
    });
    const user = userEvent.setup();

    renderAt("/mfa?return_to=%2Ffindings");
    const field = await screen.findByRole("textbox", { name: "Authentication code" });
    expect(screen.queryByRole("button", { name: /Set up/ })).not.toBeInTheDocument();
    await user.type(field, "000000");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("4 tries left");
    await user.type(field, "abcde-fghjk");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith("/findings"));
  });

  it("sends the user to sign in again when the sign-in ended", async () => {
    api({
      [STATUS]: () => json(status({ enrolled: true })),
      [VERIFY]: () => json({ detail: "Too many wrong codes. Sign in again." }, 401),
    });
    const user = userEvent.setup();

    renderAt("/mfa");
    await user.type(await screen.findByRole("textbox", { name: "Authentication code" }), "1");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many wrong codes");
    expect(screen.getByRole("link", { name: "Sign in again" })).toBeInTheDocument();
  });

  it("explains an expired sign-in", async () => {
    api({ [STATUS]: () => json({ detail: "No sign-in is waiting for a code" }, 401) });

    renderAt("/mfa");

    expect(await screen.findByRole("alert")).toHaveTextContent("expired");
    expect(screen.getByRole("link", { name: "Sign in again" })).toBeInTheDocument();
  });
});
