import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../auth/context";
import { AsOfContext } from "../asof/context";
import { routes } from "../routes";
import { analyst, signedIn } from "../test/api";
import { CapabilityGate, LARGER_SCREEN, type Capability } from "./CapabilityGate";
import { THEME_KEY } from "./theme";

/** Makes matchMedia report a phone (narrow screen) or nothing at all. */
function setDevice(device: "phone" | "desktop") {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: device === "phone" && query.includes("max-width: 767.98px"),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function gate(capability: Capability, asOf: string | null = null) {
  const session = { status: "signed_in" as const, user: analyst };
  render(
    <AuthContext.Provider value={{ session, refresh: async () => undefined, signOut: async () => undefined }}>
      <AsOfContext.Provider value={{ asOf, chosen: asOf, choose: () => undefined }}>
        <CapabilityGate capability={capability}>
          <button type="button">Act</button>
        </CapabilityGate>
      </AsOfContext.Provider>
    </AuthContext.Provider>,
  );
}

describe("theme", () => {
  beforeEach(() => {
    signedIn();
    document.documentElement.setAttribute("data-bs-theme", "dark");
    window.localStorage.removeItem(THEME_KEY);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts dark and remembers a switch to light", async () => {
    const user = userEvent.setup();
    renderAt("/");

    await user.click(await screen.findByRole("button", { name: "Use light theme" }));
    expect(document.documentElement).toHaveAttribute("data-bs-theme", "light");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("light");

    await user.click(screen.getByRole("button", { name: "Use dark theme" }));
    expect(document.documentElement).toHaveAttribute("data-bs-theme", "dark");
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
  });
});

describe("devices", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("offers decisions on phones but nothing else", () => {
    setDevice("phone");
    gate("decide");
    expect(screen.getByRole("button", { name: "Act" })).toBeInTheDocument();
  });

  it.each<Capability>(["write", "apply", "bulk", "export", "settings"])(
    "makes %s read-only on phones",
    (capability) => {
      setDevice("phone");
      gate(capability);
      expect(screen.queryByRole("button", { name: "Act" })).not.toBeInTheDocument();
      expect(screen.getByText(LARGER_SCREEN)).toBeInTheDocument();
    },
  );

  it("offers every action on larger screens", () => {
    setDevice("desktop");
    gate("apply");
    expect(screen.getByRole("button", { name: "Act" })).toBeInTheDocument();
  });

  it("turns decisions off in a past-date view, even on larger screens", () => {
    setDevice("desktop");
    gate("decide", "2026-10-01T00:00:00Z");
    expect(screen.queryByRole("button", { name: "Act" })).not.toBeInTheDocument();
  });

  it("puts the main sections in a bottom bar on phones and the rest under More", async () => {
    signedIn();
    setDevice("phone");
    const user = userEvent.setup();
    renderAt("/");

    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Dashboard",
      "Review queue",
      "Findings",
      "Inventory",
    ]);
    await user.click(within(nav).getByRole("button", { name: "More" }));
    const more = screen.getByRole("dialog", { name: "More" });
    expect(within(more).getByRole("link", { name: "Evidence exports" })).toBeInTheDocument();
  });
});
