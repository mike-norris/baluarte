import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Group, Page, Project } from "../api/gitlab";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness, kind } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const PROJECTS = "GET /api/v1/gitlab/projects?limit=100";

const seen = {
  first_seen_at: "2026-10-28T06:00:00Z",
  last_seen_at: "2026-10-29T06:00:00Z",
  last_error: null,
  last_error_at: null,
};

function project(id: number, path: string, extra: Partial<Project> = {}): Project {
  return {
    id,
    gitlab_id: id + 100,
    name: path.split("/").at(-1) ?? path,
    path_with_namespace: path,
    namespace_gitlab_id: 1,
    namespace_kind: "group",
    visibility: "private",
    archived: false,
    default_branch: "main",
    state: "active",
    ...seen,
    ...extra,
  };
}

function group(id: number, path: string, extra: Partial<Group> = {}): Group {
  return {
    id,
    gitlab_id: id + 100,
    name: path,
    full_path: path,
    parent_gitlab_id: null,
    visibility: "internal",
    state: "active",
    ...seen,
    ...extra,
  };
}

function page<T>(items: T[], next: number | null = null): Page<T> {
  return { items, next_after_id: next };
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function api(routes: Record<string, () => Response>) {
  return mockApi({
    [ME]: () => json(analyst),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

describe("inventory", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists projects with their state, visibility and collection errors", async () => {
    api({
      [PROJECTS]: () =>
        json(
          page([
            project(1, "platform/api"),
            project(2, "platform/legacy", { archived: true, visibility: null }),
            project(5, "alice/dotfiles", { namespace_kind: "user" }),
            project(3, "platform/empty", { default_branch: null, state: "missing" }),
            project(4, "platform/billing", {
              last_error: "could not confirm: GitLab returned 500 for /projects/104",
              last_error_at: "2026-10-29T06:30:00Z",
            }),
          ]),
        ),
    });

    renderAt("/inventory");

    const table = await screen.findByRole("table", { name: "GitLab projects" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(6);
    const personal = within(table).getByRole("rowheader", { name: /alice\/dotfiles/ });
    expect(personal).toHaveTextContent("Personal");
    expect(within(table).getByRole("rowheader", { name: "platform/api" })).not.toHaveTextContent(
      "Personal",
    );
    const legacy = within(table).getByRole("rowheader", { name: /platform\/legacy/ }).closest("tr");
    expect(legacy).toHaveTextContent("Archived");
    expect(legacy).toHaveTextContent("Unknown");
    const empty = within(table).getByRole("rowheader", { name: "platform/empty" }).closest("tr");
    expect(empty).toHaveTextContent("None (empty)");
    expect(empty).toHaveTextContent("Missing: Deleted, or hidden from the GitLab token.");
    const billing = within(table).getByRole("rowheader", { name: "platform/billing" }).closest("tr");
    expect(billing).toHaveTextContent("GitLab returned 500 for /projects/104");
    expect(billing).toHaveTextContent("29 Oct 2026, 06:30 UTC");
    expect(screen.getByText("Collection status: Up to date")).toBeInTheDocument();
    expect(screen.getByText("Showing 5 projects.")).toBeInTheDocument();
  });

  it("puts a failed collection first, even after an earlier success", async () => {
    api({
      [FRESHNESS]: () =>
        json(
          freshness({
            status: "failed",
            collections: {
              capabilities: kind("fresh"),
              groups: kind("fresh"),
              projects: kind("failed", {
                last_run: {
                  id: 9,
                  kind: "projects",
                  status: "failed",
                  started_at: "2026-10-29T06:55:00Z",
                  finished_at: "2026-10-29T06:56:00Z",
                  seen: 0,
                  error: "project discovery failed: GitLab rejected the token (401)",
                },
              }),
            },
          }),
        ),
      [PROJECTS]: () => json(page([project(1, "platform/api")])),
    });

    renderAt("/inventory");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The latest collection failed. The inventory below may be out of date.",
    );
    expect(screen.getByText("Collection status: Failed")).toBeInTheDocument();
    expect(
      screen.getByText(/Last run failed: project discovery failed: GitLab rejected the token/),
    ).toBeInTheDocument();
  });

  it.each([
    ["stale", "the last successful collection is more than 12 hours old"],
    ["never", "No collection has completed yet"],
    ["partial", "some resources could not be confirmed"],
  ] as const)("explains a %s collection", async (status, text) => {
    api({
      [FRESHNESS]: () => json(freshness({ status })),
      [PROJECTS]: () => json(page([])),
    });

    renderAt("/inventory");

    expect(await screen.findByText(new RegExp(text))).toBeInTheDocument();
  });

  it("says when GitLab is not configured", async () => {
    api({
      [FRESHNESS]: () => json(freshness({ status: "not_configured", collections: {} })),
      [PROJECTS]: () => json(page([])),
    });

    renderAt("/inventory");

    expect(
      await screen.findByRole("heading", { name: "GitLab is not configured" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("No projects have been collected yet.")).toBeInTheDocument();
  });

  it("reads filters from the URL and applies new ones", async () => {
    const user = userEvent.setup();
    const calls = api({
      "GET /api/v1/gitlab/projects?search=api&state=missing&limit=100": () =>
        json(page([project(3, "platform/api", { state: "missing" })])),
      "GET /api/v1/gitlab/projects?search=api&state=missing&has_error=true&limit=100&visibility=public":
        () => json(page([])),
    });
    const router = renderAt("/inventory?search=api&state=missing");

    expect(await screen.findByRole("rowheader", { name: "platform/api" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search path" })).toHaveValue("api");

    await user.selectOptions(screen.getByRole("combobox", { name: "Visibility" }), "public");
    await user.click(screen.getByRole("checkbox", { name: "With collection errors" }));
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("No projects match these filters.")).toBeInTheDocument();
    expect(router.state.location.search).toBe(
      "?search=api&state=missing&visibility=public&has_error=true",
    );
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/gitlab/projects?search=api&state=missing&has_error=true&limit=100&visibility=public",
    );

    await user.click(screen.getByRole("link", { name: "Clear" }));
    expect(router.state.location.search).toBe("");
  });

  it("switches to groups and keeps the shared filters", async () => {
    const user = userEvent.setup();
    api({
      "GET /api/v1/gitlab/projects?state=inaccessible&limit=100&visibility=private": () =>
        json(page([])),
      "GET /api/v1/gitlab/groups?state=inaccessible&limit=100": () =>
        json(page([group(1, "platform/secret", { state: "inaccessible" })])),
    });
    const router = renderAt("/inventory?state=inaccessible&visibility=private");

    await user.click(await screen.findByRole("link", { name: "Groups" }));

    const table = await screen.findByRole("table", { name: "GitLab groups" });
    expect(within(table).getByRole("rowheader", { name: "platform/secret" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?view=groups&state=inaccessible");
    expect(screen.getByRole("link", { name: "Groups" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("combobox", { name: "Visibility" })).not.toBeInTheDocument();
  });

  it("loads more pages on request", async () => {
    const user = userEvent.setup();
    api({
      [PROJECTS]: () => json(page([project(1, "a/one"), project(2, "a/two")], 2)),
      "GET /api/v1/gitlab/projects?after_id=2&limit=100": () => json(page([project(3, "a/three")])),
    });
    renderAt("/inventory");

    expect(await screen.findByText("Showing 2 projects (more available).")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more projects" }));

    expect(await screen.findByRole("rowheader", { name: "a/three" })).toBeInTheDocument();
    expect(screen.getByText("Showing 3 projects.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Load more/ })).not.toBeInTheDocument();
  });

  it("offers a retry when the API fails", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    api({
      [PROJECTS]: () => {
        attempts += 1;
        return attempts === 1 ? json({ detail: "boom" }, 500) : json(page([project(1, "a/one")]));
      },
    });
    renderAt("/inventory");

    expect(await screen.findByText("The API returned an error (500).")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("rowheader", { name: "a/one" })).toBeInTheDocument();
  });

  it("returns to sign-in when the session has ended", async () => {
    let signedIn = true;
    mockApi({
      [ME]: () => (signedIn ? json(analyst) : json({ detail: "no session" }, 401)),
      [FRESHNESS]: () => {
        signedIn = false;
        return json({ detail: "no session" }, 401);
      },
      [PROJECTS]: () => json({ detail: "no session" }, 401),
    });

    renderAt("/inventory");

    expect(
      await screen.findByRole("heading", { name: "Sign in to Baluarte" }),
    ).toBeInTheDocument();
  });
});
