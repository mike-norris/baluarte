import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Group, Page, Project } from "../api/gitlab";
import { routes } from "../routes";
import type { CurrentUser } from "../auth/session";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness, kind } from "../test/freshness";
import {
  SCOPE_ACTIVE,
  SCOPE_PROPOSED,
  excludedVia,
  exclusion,
  exclusions,
  included,
} from "../test/scope";

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
    [SCOPE_ACTIVE]: () => json(exclusions([])),
    [SCOPE_PROPOSED]: () => json(exclusions([])),
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

describe("inventory: monitoring scope", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const SHOWN = "GET /api/v1/gitlab/projects?monitoring=all&limit=100";
  const GROUPS = "GET /api/v1/gitlab/groups?limit=100";
  const auditor: CurrentUser = {
    ...analyst,
    user_id: 3,
    display_name: "Auditor",
    roles: ["auditor"],
    permissions: ["evidence:read", "findings:read", "inventory:read"],
  };
  const direct = project(4, "acme/research/prototype", {
    monitoring: excludedVia("project", 31, "acme/research/prototype"),
  });
  const byGroup = project(5, "acme/sandbox/demo", {
    monitoring: excludedVia("group", 32, "acme/sandbox"),
  });
  const personal = project(6, "someone/notes", {
    namespace_kind: "user",
    monitoring: excludedVia("personal_namespaces", 33, null),
  });
  const monitored = [
    project(1, "acme/api", { monitoring: included }),
    project(2, "acme/web", { monitoring: included }),
    project(3, "acme/docs", { monitoring: included }),
  ];
  const why = "Research prototypes are out of scope for the acme audit.";

  function posts(calls: Call[]): Call[] {
    return calls.filter((call) => call.init?.method === "POST");
  }

  it("hides excluded projects by default and shows them with how they are excluded", async () => {
    const user = userEvent.setup();
    const calls = api({
      [PROJECTS]: () => json({ ...page(monitored), excluded_hidden: 3 }),
      [SHOWN]: () => json(page([...monitored, direct, byGroup, personal])),
    });
    const router = renderAt("/inventory");

    expect(await screen.findByText("3 excluded projects hidden. Select Show excluded to see them."))
      .toBeInTheDocument();
    expect(screen.queryByRole("rowheader", { name: /prototype/ })).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Show excluded" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    await user.click(toggle);

    expect(
      await screen.findByRole("rowheader", { name: /prototype\s*Excluded directly$/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("rowheader", { name: /demo\s*Excluded with group acme\/sandbox$/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("rowheader", { name: /notes\s*Personal\s*Excluded: personal namespace$/ }),
    ).toBeInTheDocument();
    expect(router.state.location.search).toBe("?excluded=show");
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/gitlab/projects?monitoring=all&limit=100",
    );
    expect(screen.queryByText(/excluded projects hidden/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Hide excluded" }));
    expect(await screen.findByText(/3 excluded projects hidden/)).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
  });

  it("keeps showing excluded projects when filters change", async () => {
    const user = userEvent.setup();
    api({
      [SHOWN]: () => json(page([direct])),
      "GET /api/v1/gitlab/projects?search=acme&monitoring=all&limit=100": () =>
        json(page([direct])),
    });
    const router = renderAt("/inventory?excluded=show");

    expect(await screen.findByRole("button", { name: "Hide excluded" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.type(screen.getByRole("searchbox", { name: "Search path" }), "acme");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(router.state.location.search).toBe("?search=acme&excluded=show");
    expect(await screen.findByRole("rowheader", { name: /acme\/research\/prototype/ }))
      .toBeInTheDocument();
  });

  it("selects projects, announces the count and excludes them with a justification", async () => {
    const user = userEvent.setup();
    let excluded = false;
    const calls = api({
      [PROJECTS]: () => json(page(excluded ? [monitored[2] as Project] : monitored)),
      "POST /api/v1/scope/exclusions": () => {
        excluded = true;
        return json(
          {
            batch_id: "6f1c2a3e-0000-4000-8000-000000000002",
            items: [exclusion(40), exclusion(41)],
          },
          201,
        );
      },
    });
    renderAt("/inventory");

    await user.click(await screen.findByRole("checkbox", { name: "Select acme/api" }));
    expect(screen.getByText("1 project selected.")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Select all projects on this page" }));
    expect(screen.getByText("3 projects selected.")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Select acme/docs" }));
    expect(screen.getByText("2 projects selected.")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("button", { name: "Include again" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Exclude from monitoring" }));
    const form = screen.getByRole("form", { name: "Exclude from monitoring" });
    expect(within(form).getByRole("list", { name: "Selected" })).toHaveTextContent(
      "acme/apiacme/web",
    );
    await user.type(within(form).getByLabelText("Justification"), "Too short.");
    await user.click(within(form).getByRole("button", { name: "Exclude" }));
    expect(within(form).getByRole("alert")).toHaveTextContent(
      "Justification: Explain the reason in at least 20 characters.",
    );
    expect(posts(calls)).toEqual([]);

    await user.clear(within(form).getByLabelText("Justification"));
    await user.type(within(form).getByLabelText("Justification"), why);
    await user.click(within(form).getByRole("button", { name: "Exclude" }));

    expect(await screen.findByText("Excluded from monitoring: 2 exclusions, in force now."))
      .toBeInTheDocument();
    const [post] = posts(calls);
    expect(post?.url).toBe("/api/v1/scope/exclusions");
    expect(post?.init?.body).toBe(
      JSON.stringify({
        targets: [
          { type: "project", gitlab_id: 101 },
          { type: "project", gitlab_id: 102 },
        ],
        personal_namespaces: false,
        justification: why,
      }),
    );
    expect(await screen.findByText("Showing 1 projects.")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Exclude from monitoring" })).not.toBeInTheDocument();
  });

  it("says when an exclusion waits for review, and explains a refusal", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    api({
      [PROJECTS]: () => json(page(monitored)),
      "POST /api/v1/scope/exclusions": () => {
        attempt += 1;
        return attempt === 1
          ? json(
              {
                detail: {
                  code: "already_excluded",
                  message: "Already excluded",
                  reasons: ["already_excluded"],
                },
              },
              409,
            )
          : json(
              {
                batch_id: "6f1c2a3e-0000-4000-8000-000000000003",
                items: [exclusion(42, { state: "proposed", review_required: true })],
              },
              201,
            );
      },
    });
    renderAt("/inventory");

    await user.click(await screen.findByRole("checkbox", { name: "Select acme/api" }));
    await user.click(screen.getByRole("button", { name: "Exclude from monitoring" }));
    const form = screen.getByRole("form", { name: "Exclude from monitoring" });
    await user.type(within(form).getByLabelText("Justification"), why);
    await user.click(within(form).getByRole("button", { name: "Exclude" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Not possible now: a selected project or group is already excluded or waiting for review.",
    );

    await user.click(within(form).getByRole("button", { name: "Exclude" }));
    expect(
      await screen.findByText(
        "Proposed for review: 1 exclusion. Monitoring changes once someone else approves.",
      ),
    ).toBeInTheDocument();
  });

  it("includes again only projects excluded directly", async () => {
    const user = userEvent.setup();
    const calls = api({
      [SHOWN]: () => json(page([direct, byGroup])),
      "POST /api/v1/scope/exclusions/end": () => json({ items: [] }),
    });
    renderAt("/inventory?excluded=show");

    await user.click(
      await screen.findByRole("checkbox", { name: "Select all projects on this page" }),
    );
    expect(screen.getByText("2 projects selected.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "1 selected project is excluded by a group or the personal-namespace setting; include them again under Monitoring scope.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exclude from monitoring" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Include again" }));
    const form = screen.getByRole("form", { name: "Include in monitoring again" });
    expect(within(form).getByRole("list", { name: "Selected" })).toHaveTextContent(
      "acme/research/prototype",
    );
    expect(within(form).getByRole("list", { name: "Selected" })).not.toHaveTextContent("demo");
    await user.type(within(form).getByLabelText("Justification"), "The prototype became a product.");
    await user.click(within(form).getByRole("button", { name: "Include again" }));

    expect(
      await screen.findByText(
        "Included in monitoring again: 1 exclusion ended. The next evaluation checks them.",
      ),
    ).toBeInTheDocument();
    const [post] = posts(calls);
    expect(post?.url).toBe("/api/v1/scope/exclusions/end");
    expect(post?.init?.body).toBe(
      JSON.stringify({ ids: [31], justification: "The prototype became a product." }),
    );
  });

  it("excludes a whole group from the groups list", async () => {
    const user = userEvent.setup();
    const calls = api({
      [GROUPS]: () => json(page([group(7, "acme/sandbox", { monitoring: included })])),
      "POST /api/v1/scope/exclusions": () =>
        json({ batch_id: "6f1c2a3e-0000-4000-8000-000000000004", items: [exclusion(43)] }, 201),
    });
    renderAt("/inventory?view=groups");

    await user.click(await screen.findByRole("checkbox", { name: "Select acme/sandbox" }));
    expect(screen.getByText("1 group selected.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Exclude from monitoring" }));
    const form = screen.getByRole("form", { name: "Exclude from monitoring" });
    await user.type(within(form).getByLabelText("Justification"), why);
    await user.click(within(form).getByRole("button", { name: "Exclude" }));

    expect(await screen.findByText(/Excluded from monitoring: 1 exclusion/)).toBeInTheDocument();
    const [post] = posts(calls);
    expect(JSON.parse(String(post?.init?.body))).toMatchObject({
      targets: [{ type: "group", gitlab_id: 107 }],
    });
  });

  it("lists exclusions in the Monitoring scope section and excludes personal namespaces", async () => {
    const user = userEvent.setup();
    let personalExcluded = false;
    const calls = api({
      [PROJECTS]: () => json(page(monitored)),
      [SCOPE_ACTIVE]: () =>
        json(
          exclusions([
            exclusion(31),
            ...(personalExcluded
              ? [exclusion(33, { target_type: "personal_namespaces", target_id: null, target_path: null })]
              : []),
          ]),
        ),
      "POST /api/v1/scope/exclusions": () => {
        personalExcluded = true;
        return json(
          {
            batch_id: "6f1c2a3e-0000-4000-8000-000000000005",
            items: [exclusion(33, { target_type: "personal_namespaces", target_id: null })],
          },
          201,
        );
      },
    });
    renderAt("/inventory");

    const scope = await screen.findByRole("region", { name: "Monitoring scope" });
    const list = await within(scope).findByRole("list", { name: "Exclusions" });
    expect(list).toHaveTextContent("Project acme/research/prototype");
    expect(list).toHaveTextContent(why);
    expect(list).toHaveTextContent("Excluded by Ada Lovelace");
    expect(list).toHaveTextContent("not reviewed, review was off.");
    expect(
      within(list).getByRole("button", { name: "Include again: Project acme/research/prototype" }),
    ).toBeInTheDocument();

    await user.click(
      within(scope).getByRole("button", { name: "Exclude all personal-namespace projects" }),
    );
    const form = within(scope).getByRole("form", { name: "Exclude from monitoring" });
    expect(form).toHaveTextContent("including ones discovered later");
    await user.type(within(form).getByLabelText("Justification"), "Personal projects are not company work.");
    await user.click(within(form).getByRole("button", { name: "Exclude" }));

    expect(await screen.findByText("Excluded from monitoring: 1 exclusion, in force now."))
      .toBeInTheDocument();
    const [post] = posts(calls);
    expect(post?.init?.body).toBe(
      JSON.stringify({
        targets: [],
        personal_namespaces: true,
        justification: "Personal projects are not company work.",
      }),
    );
    expect(await within(scope).findByText("All personal-namespace projects")).toBeInTheDocument();
    expect(
      within(scope).queryByRole("button", { name: "Exclude all personal-namespace projects" }),
    ).not.toBeInTheDocument();
  });

  it("shows read-only users the badges but no way to change the scope", async () => {
    mockApi({
      [ME]: () => json(auditor),
      [FRESHNESS]: () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
      [SCOPE_ACTIVE]: () => json(exclusions([exclusion(31)])),
      [SCOPE_PROPOSED]: () => json(exclusions([])),
      [SHOWN]: () => json(page([monitored[0] as Project, direct])),
    });
    renderAt("/inventory?excluded=show");

    expect(
      await screen.findByRole("rowheader", { name: /prototype\s*Excluded directly$/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /^Select/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Exclude from monitoring" })).not.toBeInTheDocument();
    const scope = screen.getByRole("region", { name: "Monitoring scope" });
    expect(await within(scope).findByText(why)).toBeInTheDocument();
    expect(within(scope).queryByRole("button", { name: /Include again/ })).not.toBeInTheDocument();
    expect(
      within(scope).queryByRole("button", { name: "Exclude all personal-namespace projects" }),
    ).not.toBeInTheDocument();
  });
});
