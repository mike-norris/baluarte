import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navItems } from "./navigation";
import { routes } from "./routes";
import { analyst, signedIn } from "./test/api";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("navigation", () => {
  beforeEach(() => {
    signedIn();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists every section the user may see in the primary nav", async () => {
    renderAt("/");

    const nav = await screen.findByRole("navigation", { name: "Primary" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(
      navItems.filter((item) => item.path !== "/settings").map((item) => item.label),
    );
  });

  it("shows Settings only to users who manage settings", async () => {
    vi.unstubAllGlobals();
    signedIn({
      ...analyst,
      roles: ["admin"],
      permissions: [...analyst.permissions, "settings:manage"],
    });

    renderAt("/policies");

    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(navItems.map((item) => item.label));
  });

  it("marks only the current section with aria-current", async () => {
    renderAt("/inventory");

    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(nav).getByRole("link", { name: "Inventory" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).getByRole("link", { name: "Dashboard" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("navigates between sections", async () => {
    const user = userEvent.setup();
    const router = renderAt("/");

    await user.click(await screen.findByRole("link", { name: "Findings" }));

    expect(router.state.location.pathname).toBe("/findings");
    expect(screen.getByRole("heading", { level: 1, name: "Findings" })).toBeInTheDocument();
  });

  it.each(navItems)("renders a page heading for $label", async ({ path, label }) => {
    renderAt(path);

    expect(await screen.findByRole("heading", { level: 1, name: label })).toBeInTheDocument();
  });

  it("shows a not-found page for unknown paths", () => {
    renderAt("/does-not-exist");

    expect(screen.getByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Return to the dashboard" })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("offers a skip link to the main landmark", () => {
    renderAt("/");

    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute(
      "href",
      "#main",
    );
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });
});
