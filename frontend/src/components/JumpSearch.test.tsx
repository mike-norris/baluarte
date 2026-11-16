import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const GROUPS = "GET /api/v1/gitlab/groups?search=platform&state=active&monitoring=all&limit=100";
const PROJECTS =
  "GET /api/v1/gitlab/projects?search=platform&state=active&monitoring=all&limit=100";

function api(routes: Record<string, () => Response> = {}) {
  return mockApi({
    [ME]: () => json(analyst),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("jump search", () => {
  it("opens a record by number with the keyboard", async () => {
    const user = userEvent.setup();
    api();
    const router = renderAt("/settings");

    const input = await screen.findByRole("combobox", {
      name: "Jump to a page, record or project",
    });
    await user.type(input, "#12");
    const list = screen.getByRole("listbox", { name: "Jump results" });
    expect(within(list).getByRole("option", { name: /Finding #12/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await user.keyboard("{Enter}");
    expect(router.state.location.pathname).toBe("/findings/12");
  });

  it("finds pages by name and projects and groups by path", async () => {
    const user = userEvent.setup();
    api({
      [GROUPS]: () =>
        json({ items: [{ gitlab_id: 7, full_path: "platform" }], next_after_id: null }),
      [PROJECTS]: () =>
        json({
          items: [{ gitlab_id: 9, path_with_namespace: "platform/api" }],
          next_after_id: null,
        }),
    });
    const router = renderAt("/settings");

    const input = await screen.findByRole("combobox", {
      name: "Jump to a page, record or project",
    });
    await user.type(input, "platform");
    const project = await screen.findByRole("option", { name: /platform\/api/ });
    expect(screen.getByRole("option", { name: /^platform\s*Group$/ })).toBeInTheDocument();

    await user.click(project);
    expect(router.state.location.pathname).toBe("/inventory/projects/9");
  });

  it("closes on Escape and says when nothing matches", async () => {
    const user = userEvent.setup();
    api();
    renderAt("/settings");

    const input = await screen.findByRole("combobox", {
      name: "Jump to a page, record or project",
    });
    await user.type(input, "#x");
    expect(screen.getByText("No matches.")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(input).toHaveAttribute("aria-expanded", "false");
  });
});

describe("keyboard shortcuts", () => {
  it("opens pages with g then a key, and lists the shortcuts under ?", async () => {
    const user = userEvent.setup();
    api();
    const router = renderAt("/settings");
    await screen.findByRole("combobox", { name: "Jump to a page, record or project" });

    await user.keyboard("gf");
    expect(router.state.location.pathname).toBe("/findings");

    await user.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(dialog).getByText("Open review queue")).toBeInTheDocument();
  });

  it("does nothing while typing in a field, and / focuses the search", async () => {
    const user = userEvent.setup();
    api();
    const router = renderAt("/settings");
    const input = await screen.findByRole("combobox", {
      name: "Jump to a page, record or project",
    });

    await user.keyboard("/");
    expect(input).toHaveFocus();
    await user.keyboard("gf");
    expect(router.state.location.pathname).toBe("/settings");
    expect(input).toHaveValue("gf");
  });

  it("is off on phones, where there is no jump search", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width: 767.98px"),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    api();
    const router = renderAt("/settings");
    await screen.findByRole("heading", { name: "Settings" });

    expect(
      screen.queryByRole("combobox", { name: "Jump to a page, record or project" }),
    ).not.toBeInTheDocument();
    await act(async () => {
      await userEvent.keyboard("gf");
    });
    expect(router.state.location.pathname).toBe("/settings");
  });
});
