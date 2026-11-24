import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MfaEnrollmentRow } from "../api/mfa";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
  csrf_token: "a".repeat(64),
};
const ENROLLMENTS = "/api/v1/settings/mfa/enrollments";

function row(userId: number, name: string): MfaEnrollmentRow {
  return {
    user_id: userId,
    email: `${name.toLowerCase()}@example.test`,
    display_name: name,
    confirmed_at: "2026-11-20T10:00:00Z",
    recovery_codes_left: 10,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("authenticator apps", () => {
  it("resets another person's authenticator after confirming, never one's own", async () => {
    let rows = [row(1, "Ada"), row(2, "Grace")];
    const calls = mockApi({
      "GET /api/v1/auth/me": () => json(admin),
      [`GET ${ENROLLMENTS}`]: () => json(rows),
      [`DELETE ${ENROLLMENTS}/2`]: () => {
        rows = [row(1, "Ada")];
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    render(
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/settings"] })} />,
    );

    const card = (await screen.findByRole("heading", { name: "Authenticator apps" })).closest(
      "section",
    ) as HTMLElement;
    const table = await within(card).findByRole("table");
    expect(within(table).queryByRole("button", { name: "Reset Ada's authenticator" })).toBeNull();
    await user.click(within(table).getByRole("button", { name: "Reset Grace's authenticator" }));
    await user.click(within(table).getByRole("button", { name: "Confirm reset for Grace" }));

    expect(await within(card).findByRole("status")).toHaveTextContent("Grace's authenticator");
    expect(calls.some((c) => c.init?.method === "DELETE")).toBe(true);
    await vi.waitFor(() => expect(within(card).queryByText("Grace")).toBeNull());
  });
});
