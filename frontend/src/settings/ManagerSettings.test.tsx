import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Group, Page } from "../api/gitlab";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { SCOPES_ACTIVE, SETTINGS_TIME, USERS, scope, timeSettings, users } from "../test/manager";

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
  csrf_token: "a".repeat(64),
};

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(routes: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(admin),
    [SCOPES_ACTIVE]: () => json({ items: [scope(1, "acme/payments")] }),
    [USERS]: () => json({ items: users }),
    [SETTINGS_TIME]: () => json(timeSettings()),
    ...routes,
  });
}

function renderSettings() {
  const router = createMemoryRouter(routes, { initialEntries: ["/settings"] });
  render(<RouterProvider router={router} />);
}

function bodyOf(calls: Call[], method: string, url: string): unknown {
  const call = calls.filter((c) => c.url === url && c.init?.method === method).at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("manager groups", () => {
  it("lists active assignments and ends one", async () => {
    const calls = api({
      "POST /api/v1/manager/scopes/1/end": () =>
        json({
          ...scope(1, "acme/payments"),
          ended_at: "2026-11-06T10:00:00Z",
        }),
    });
    const user = userEvent.setup();
    renderSettings();

    const card = await screen.findByRole("region", { name: "Manager groups" });
    const table = await within(card).findByRole("table", {
      name: "Groups assigned to managers",
    });
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("Grace Hopperacme/payments");
    await user.click(
      within(table).getByRole("button", {
        name: "End acme/payments for Grace Hopper",
      }),
    );

    expect(
      await within(card).findByText("Grace Hopper no longer has acme/payments."),
    ).toBeInTheDocument();
    expect(bodyOf(calls, "POST", "/api/v1/manager/scopes/1/end")).toEqual({});
  });

  it("assigns a group to someone with the manager role only", async () => {
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
    const calls = api({
      "GET /api/v1/gitlab/groups?search=acme%2Fweb&state=active&monitoring=all&limit=100": () =>
        json(groups),
      "POST /api/v1/manager/scopes": () => json({ items: [scope(2, "acme/web")] }, 201),
    });
    const user = userEvent.setup();
    renderSettings();

    const card = await screen.findByRole("region", { name: "Manager groups" });
    const manager = await within(card).findByRole("combobox", {
      name: "Manager",
    });
    await within(card).findByRole("option", { name: "Grace Hopper" });
    expect(within(manager).queryByRole("option", { name: /Ada Lovelace/ })).toBeNull();

    await user.click(within(card).getByRole("button", { name: "Assign group" }));
    expect(within(card).getByRole("alert")).toHaveTextContent("Choose a manager.Choose a group.");

    await user.selectOptions(manager, "3");
    await user.type(within(card).getByLabelText("Find a group by path"), "acme/web");
    await user.click(within(card).getByRole("button", { name: "Search groups" }));
    await user.click(await within(card).findByRole("radio", { name: /acme\/web/ }));
    await user.click(within(card).getByRole("button", { name: "Assign group" }));

    expect(await within(card).findByText("Grace Hopper now has acme/web.")).toBeInTheDocument();
    expect(bodyOf(calls, "POST", "/api/v1/manager/scopes")).toEqual({
      user_id: 3,
      group_gitlab_ids: [115],
    });
  });
});

describe("time tracking settings", () => {
  it("saves limits, with empty retention following evidence", async () => {
    const calls = api({
      "PUT /api/v1/settings/time": () => json(timeSettings({ lookback_days: 365 })),
    });
    const user = userEvent.setup();
    renderSettings();

    const card = await screen.findByRole("region", { name: "Time tracking" });
    expect(await within(card).findByText(/Time per person is shown to managers/)).toBeVisible();
    const lookback = within(card).getByLabelText("Lookback (days)");
    await user.clear(lookback);
    await user.type(lookback, "900");
    await user.click(within(card).getByRole("button", { name: "Save time tracking settings" }));
    expect(within(card).getByRole("alert")).toHaveTextContent(
      "Lookback must be a whole number from 1 to 730.",
    );

    await user.clear(lookback);
    await user.type(lookback, "365");
    await user.click(within(card).getByRole("button", { name: "Save time tracking settings" }));

    expect(await within(card).findByText(/Time tracking settings saved/)).toBeInTheDocument();
    expect(bodyOf(calls, "PUT", "/api/v1/settings/time")).toEqual({
      lookback_days: 365,
      reread_days: 30,
      retention_days: null,
    });
  });

  it("says when the deployment turns time per person off", async () => {
    api({
      [SETTINGS_TIME]: () => json(timeSettings({ per_person_enabled: false })),
    });
    renderSettings();
    const card = await screen.findByRole("region", { name: "Time tracking" });
    expect(await within(card).findByText(/Time per person is turned off/)).toBeVisible();
  });
});
