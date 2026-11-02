import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  isChunkLoadError,
  reloadForStaleChunk,
  RELOAD_WINDOW_MS,
  withStaleChunkReload,
} from "./chunkReload";
import { RouteErrorPage } from "./pages/RouteErrorPage";

const stale = new TypeError(
  "Failed to fetch dynamically imported module: http://127.0.0.1:8090/assets/Page-abc.js",
);

afterEach(() => {
  sessionStorage.clear();
});

describe("stale page bundles", () => {
  it("recognises each browser's wording, and nothing else", () => {
    expect(isChunkLoadError(stale)).toBe(true);
    expect(isChunkLoadError(new TypeError("error loading dynamically imported module"))).toBe(true);
    expect(isChunkLoadError(new TypeError("Importing a module script failed."))).toBe(true);
    expect(isChunkLoadError(new Error("Cannot read properties of undefined"))).toBe(false);
  });

  it("reloads once, then lets a repeat failure through", () => {
    const reload = vi.fn();
    expect(reloadForStaleChunk(stale, 1_000, reload)).toBe(true);
    expect(reloadForStaleChunk(stale, 1_000 + RELOAD_WINDOW_MS - 1, reload)).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
    // Long after, a new deployment may have happened again.
    expect(reloadForStaleChunk(stale, 1_000 + RELOAD_WINDOW_MS, reload)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("never reloads for other errors", () => {
    const reload = vi.fn();
    expect(reloadForStaleChunk(new Error("boom"), 1_000, reload)).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  it("does not reload when the loop guard cannot be stored", () => {
    const reload = vi.fn();
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(reloadForStaleChunk(stale, 1_000, reload)).toBe(false);
    expect(reload).not.toHaveBeenCalled();
    setItem.mockRestore();
  });

  it("passes other import failures through to the route", async () => {
    const load = withStaleChunkReload(() => Promise.reject(new Error("syntax error in module")));
    await expect(load()).rejects.toThrow("syntax error in module");
  });

  it("returns the module when the import succeeds", async () => {
    const load = withStaleChunkReload(() => Promise.resolve({ Page: "ok" }));
    await expect(load()).resolves.toEqual({ Page: "ok" });
  });
});

function renderError(error: unknown) {
  const router = createMemoryRouter([
    {
      path: "/",
      loader: () => {
        throw error;
      },
      element: <p>never</p>,
      errorElement: <RouteErrorPage />,
    },
  ]);
  render(<RouterProvider router={router} />);
}

describe("route error page", () => {
  it("explains a stale bundle as an update and offers a reload", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    renderError(stale);
    expect(
      await screen.findByRole("heading", { name: "Baluarte has been updated" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to the dashboard" })).toHaveAttribute("href", "/");
  });

  it("shows a generic message for other errors, without the error text", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    renderError(new Error("internal detail"));
    expect(
      await screen.findByRole("heading", { name: "Something went wrong" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/internal detail/)).not.toBeInTheDocument();
  });
});
