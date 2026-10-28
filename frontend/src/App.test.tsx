import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { navItems } from "./navigation";
import { routes } from "./routes";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("navigation", () => {
  it("lists every section in the primary nav", () => {
    renderAt("/");

    const nav = screen.getByRole("navigation", { name: "Primary" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(navItems.map((item) => item.label));
  });

  it("marks only the current section with aria-current", () => {
    renderAt("/inventory");

    const nav = screen.getByRole("navigation", { name: "Primary" });
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

    await user.click(screen.getByRole("link", { name: "Findings" }));

    expect(router.state.location.pathname).toBe("/findings");
    expect(screen.getByRole("heading", { level: 1, name: "Findings" })).toBeInTheDocument();
  });

  it.each(navItems)("renders a page heading for $label", ({ path, label }) => {
    renderAt(path);

    expect(screen.getByRole("heading", { level: 1, name: label })).toBeInTheDocument();
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
