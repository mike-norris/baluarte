import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Release } from "../api/releases";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { peopleText } from "./ReleasesPage";

const release: Release = {
  change_id: 12,
  connection_id: 1,
  project_id: 3,
  project_path: "platform/api",
  deployment_id: 300,
  environment: "production",
  ref: "v1-release",
  tag: true,
  sha: "a".repeat(40),
  finished_at: "2026-11-24T10:00:00Z",
  deployer: "ops",
  started_by: "dev",
  approvers: ["lead"],
  release_name: "Release 1",
};
const packager: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "evidence:package"],
};

function api(user: CurrentUser): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/releases?limit=100": () => json([release]),
    "POST /api/v1/releases/exports": () =>
      new Response("PK", {
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": 'attachment; filename="baluarte-release-platform-api-300.zip"',
          "X-Baluarte-Export-Id": "e".repeat(32),
        },
      }),
  });
}

function renderPage() {
  const router = createMemoryRouter(routes, { initialEntries: ["/governance/releases"] });
  render(<RouterProvider router={router} />);
}

let saved: string[] = [];

beforeEach(() => {
  saved = [];
  URL.createObjectURL = vi.fn(() => "blob:package");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Releases", () => {
  it("names who started, approved and deployed a release", () => {
    expect(peopleText(release)).toBe("started by dev; approved by lead; deployed by ops");
    expect(peopleText({ ...release, approvers: null, started_by: null })).toBe(
      "started by not known; approvals not read; deployed by ops",
    );
  });

  it("exports an audit release with or without reports and says how to verify it", async () => {
    const calls = api(packager);
    renderPage();

    expect(await screen.findByText("platform/api")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /Include the release pipeline/ }));
    await userEvent.click(screen.getByRole("button", { name: "Export audit release" }));

    expect(
      await screen.findByText(/Downloaded baluarte-release-platform-api-300.zip/),
    ).toBeVisible();
    expect(saved).toEqual(["baluarte-release-platform-api-300.zip"]);
    const sent = calls.find((c) => c.init?.method === "POST");
    expect(JSON.parse(String(sent?.init?.body))).toEqual({
      change_id: 12,
      include_reports: false,
    });
  });

  it("offers no export without the package permission", async () => {
    api(analyst);
    renderPage();

    expect(await screen.findByText("platform/api")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Export audit release" })).toBeNull();
    expect(screen.getByRole("link", { name: "Releases" })).toHaveAttribute("aria-current", "page");
  });
});
