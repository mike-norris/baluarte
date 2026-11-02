import { render, screen, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CollectionAlert, FreshnessReport, Population } from "../api/gitlab";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const RUNS = "GET /api/v1/evaluations/runs?limit=100";
const OPEN = "GET /api/v1/findings?state=open&limit=200";
const PROJECTS = "GET /api/v1/gitlab/projects?limit=100";

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
};

const failing: CollectionAlert = {
  reason: "failing",
  kinds: ["capabilities", "groups", "projects"],
  consecutive_failures: 4,
  failing_since: "2026-10-31T19:00:47Z",
  last_success_at: "2026-10-31T18:00:50Z",
  last_error: "GitLab could not be reached",
  message: "GitLab collection has failed 4 times in a row.",
};

const member: Population = {
  basis: "token_visibility",
  token_username: "auditor",
  is_admin: false,
  statement:
    "Inventory covers only the groups and projects visible to auditor. auditor is not a GitLab " +
    "administrator, so projects this account cannot see are not included and are not checked.",
};

const instanceAdmin: Population = {
  basis: "instance",
  token_username: "root",
  is_admin: true,
  statement: "Inventory covers every group and project on the instance.",
};

function api(report: FreshnessReport, user: CurrentUser = analyst) {
  return mockApi({
    [ME]: () => json(user),
    [FRESHNESS]: () => json(report),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [RUNS]: () => json({ items: [], next_before_id: null }),
    [OPEN]: () => json({ items: [], next_after_id: null }),
    [PROJECTS]: () => json({ items: [], next_after_id: null }),
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("collection health banner", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("says since when collection has failed and when it last succeeded", async () => {
    api(freshness({ alert: failing }), admin);
    renderAt("/");

    const banner = await screen.findByText(/GitLab collection has failed 4 times in a row since/);
    const alert = banner.closest("[role=alert]") as HTMLElement;
    expect(alert).toHaveTextContent(/The last successful collection was/);
    expect(alert).toHaveTextContent("Check that Baluarte can reach GitLab");
    expect(alert).toHaveTextContent("Last error: GitLab could not be reached");
    expect(
      within(alert).getByRole("link", { name: "Check the GitLab connection in Settings" }),
    ).toHaveAttribute("href", "/settings");
  });

  it("points other roles to their administrator", async () => {
    api(freshness({ alert: failing }));
    renderAt("/inventory");

    const banner = await screen.findByText(/has failed 4 times in a row/);
    const alert = banner.closest("[role=alert]") as HTMLElement;
    expect(alert).toHaveTextContent("Tell your Baluarte administrator.");
    expect(within(alert).queryByRole("link", { name: /Settings/ })).not.toBeInTheDocument();
  });

  it("explains an overdue collection", async () => {
    api(
      freshness({
        alert: { ...failing, reason: "overdue", consecutive_failures: 0, failing_since: null },
      }),
    );
    renderAt("/");

    expect(await screen.findByText("GitLab collection is overdue.")).toBeInTheDocument();
    expect(screen.getByText(/Check that the Baluarte worker is running/)).toBeInTheDocument();
  });

  it("is not shown while collection is healthy", async () => {
    api(freshness({ alert: null, population: member }));
    renderAt("/");

    expect(await screen.findByLabelText("Inventory coverage")).toBeInTheDocument();
    expect(screen.queryByText(/GitLab collection has failed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/GitLab collection is overdue/)).not.toBeInTheDocument();
  });
});

describe("inventory population", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([
    ["/", member],
    ["/inventory", member],
    ["/", instanceAdmin],
    ["/inventory", instanceAdmin],
  ])("states the population on %s", async (path, population) => {
    api(freshness({ population }));
    renderAt(path);

    expect(await screen.findByLabelText("Inventory coverage")).toHaveTextContent(
      population.statement,
    );
  });
});
