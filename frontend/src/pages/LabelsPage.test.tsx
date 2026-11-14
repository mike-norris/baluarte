import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import {
  DRIFT,
  TAXONOMY,
  drift,
  driftPage,
  ownerLabels,
  taxonomyVersion,
  taxonomyView,
} from "../test/labels";

const AS_OF = "2026-10-01T00:00:00Z";
const planner: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "remediation:plan"],
};
const reader: CurrentUser = {
  ...analyst,
  permissions: ["evidence:read", "findings:read", "inventory:read"],
};

function api(user: CurrentUser, routes: Record<string, () => Response>): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

const unknown = drift("unknown", {
  id: "project:106:unknown:-",
  owner: { type: "project", gitlab_id: 106, path: "acme/web" },
  label: null,
  observed: null,
  expected: null,
  reason: "GitLab answered 403",
  observations: [],
});

describe("labels page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the taxonomy with each colour as text beside its swatch", async () => {
    api(planner, {
      [TAXONOMY]: () => json(taxonomyView()),
      [DRIFT]: () => json(driftPage([])),
    });
    renderAt("/labels");

    const table = await screen.findByRole("table", { name: "Labels in the taxonomy" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("priority::high#d9534fFix this sprintP1");
    expect(rows[2]).toHaveTextContent("type::bug#1f75cbNone");
    expect(screen.getByText("acme-labels v1 (In force)")).toBeInTheDocument();
    expect(screen.getByText("Approved by Grace Hopper", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Propose a change to the taxonomy" })).toHaveAttribute(
      "href",
      "/labels/taxonomy/new",
    );
    expect(
      within(screen.getByRole("main")).getByRole("link", { name: "Label plans" }),
    ).toHaveAttribute("href", "/labels/plans");
    expect(screen.getByRole("link", { name: "Label consistency findings" })).toHaveAttribute(
      "href",
      "/findings?rule_type=gitlab.labels.consistent",
    );
    expect(
      await screen.findByText("No drift: every group and project in scope matches the taxonomy."),
    ).toBeInTheDocument();
  });

  it("lists drift with what was observed and expected, and unknown as unknown", async () => {
    api(reader, {
      [TAXONOMY]: () =>
        json(
          taxonomyView({
            versions: [taxonomyVersion(2, { state: "proposed" }), taxonomyVersion(1)],
          }),
        ),
      [DRIFT]: () => json(driftPage([drift("color"), unknown])),
    });
    renderAt("/labels");

    const table = await screen.findByRole("table", { name: "Label drift by group or project" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("acme/apiProject");
    expect(rows[1]).toHaveTextContent("Colour differs");
    expect(rows[1]).toHaveTextContent("#ff0000");
    expect(rows[1]).toHaveTextContent("Defined on Group acme");
    expect(rows[1]).toHaveTextContent("#d9534f");
    expect(rows[1]).toHaveTextContent("Observation 501");
    expect(within(rows[1] as HTMLElement).getByRole("link", { name: "acme/api" })).toHaveAttribute(
      "href",
      "/labels/owners/project/105",
    );
    expect(rows[2]).toHaveTextContent("Unknown: GitLab answered 403");
    expect(rows[2]).toHaveTextContent("No observation");
    expect(screen.getByText("1 proposed taxonomy is waiting for review.")).toBeInTheDocument();
    const byKind = screen.getByRole("region", { name: "Drift by kind (2 in all)" });
    expect(within(byKind).getByRole("link", { name: "Colour differs" })).toHaveAttribute(
      "href",
      "/labels?kind=color",
    );
    // Without the baseline writing permission there is nothing to propose or plan.
    expect(screen.queryByRole("link", { name: /Propose a change/ })).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("main")).queryByRole("link", { name: "Label plans" }),
    ).not.toBeInTheDocument();
  });

  it("filters drift through the URL and loads more pages after the last item", async () => {
    const calls = api(analyst, {
      [TAXONOMY]: () => json(taxonomyView()),
      [DRIFT]: () => json(driftPage([drift("color")])),
      "GET /api/v1/labels/drift?group=acme&kind=missing&limit=50": () =>
        json(driftPage([drift("missing")], { next_after: "project:105:missing:priority::high" })),
      [`GET /api/v1/labels/drift?group=acme&kind=missing&after=${encodeURIComponent("project:105:missing:priority::high")}&limit=50`]:
        () =>
          json(
            driftPage([drift("missing", { id: "group:10:missing:type::bug", label: "type::bug" })]),
          ),
    });
    const user = userEvent.setup();
    const router = renderAt("/labels");

    const form = await screen.findByRole("form", { name: "Filter label drift" });
    await user.type(within(form).getByLabelText("Top-level group"), "acme");
    await user.selectOptions(within(form).getByLabelText("Kind"), "missing");
    await user.click(within(form).getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByRole("button", { name: "Load more drift" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("?group=acme&kind=missing");
    await user.click(screen.getByRole("button", { name: "Load more drift" }));
    const table = screen.getByRole("table", { name: "Label drift by group or project" });
    expect(await within(table).findByText("type::bug")).toBeInTheDocument();
    expect(calls.map((call) => call.url)).toContain(
      `/api/v1/labels/drift?group=acme&kind=missing&after=${encodeURIComponent("project:105:missing:priority::high")}&limit=50`,
    );
  });

  it("reads the taxonomy and drift as of a past date and turns proposing off", async () => {
    const run = { id: 12, finished_at: "2026-09-30T23:00:00Z" };
    const calls = api(planner, {
      [`${TAXONOMY}?as_of=${encodeURIComponent(AS_OF)}`]: () =>
        json(taxonomyView({ as_of: AS_OF, evaluation_run: run })),
      [`GET /api/v1/labels/drift?as_of=${encodeURIComponent(AS_OF)}&limit=50`]: () =>
        json(driftPage([drift("color")], { as_of: AS_OF, evaluation_run: run })),
    });
    renderAt(`/labels?as_of=${AS_OF}`);

    expect(await screen.findByText(/from evaluation run 12/)).toBeInTheDocument();
    expect(screen.getAllByText(/You are viewing the past/).length).toBeGreaterThan(0);
    expect(screen.queryByRole("link", { name: /Propose a change/ })).not.toBeInTheDocument();
    expect(
      calls.every((call) => !call.url.startsWith("/api/v1/labels") || call.url.includes("as_of=")),
    ).toBe(true);
  });

  it("says when no taxonomy is in force", async () => {
    api(planner, {
      [TAXONOMY]: () => json(taxonomyView({ current: null, versions: [] })),
      [DRIFT]: () => json(driftPage([], { taxonomy: null, collected_at: null })),
    });
    renderAt("/labels");

    expect(await screen.findByText(/No label taxonomy is in force/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Define the taxonomy" })).toBeInTheDocument();
    expect(
      await screen.findByText(
        "No taxonomy was in force, so there is nothing to measure drift against.",
      ),
    ).toBeInTheDocument();
  });
});

describe("label owner page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists the labels available, where each is defined, and the drift", async () => {
    api(analyst, { "GET /api/v1/labels/owners/project/105": () => json(ownerLabels()) });
    renderAt("/labels/owners/project/105");

    expect(await screen.findByRole("heading", { name: "Project acme/api" })).toBeInTheDocument();
    const labels = screen.getByRole("table", { name: "Labels available here" });
    const rows = within(labels).getAllByRole("row");
    expect(
      within(rows[1] as HTMLElement).getByRole("link", { name: "Group acme" }),
    ).toHaveAttribute("href", "/labels/owners/group/10");
    expect(rows[2]).toHaveTextContent("local#00aa00Project onlyHere");
    expect(screen.getByRole("table", { name: "Drift on acme/api" })).toHaveTextContent(
      "Colour differs",
    );
    expect(screen.getByText(`SHA-256 ${"e".repeat(64)}`)).toBeInTheDocument();
  });

  it("shows unreadable labels as unknown, not as none", async () => {
    api(analyst, {
      "GET /api/v1/labels/owners/group/10": () =>
        json(
          ownerLabels({
            owner: { type: "group", gitlab_id: 10, path: "acme" },
            status: "unknown",
            reason: "GitLab answered 403",
            observation: null,
            labels: [],
            drift: [],
          }),
        ),
    });
    renderAt("/labels/owners/group/10");

    expect(
      await screen.findByText(/Unknown: the labels here could not be read \(GitLab answered 403\)/),
    ).toBeInTheDocument();
    expect(screen.getByText("Not known: the labels could not be read.")).toBeInTheDocument();
  });

  it("shows not found for a malformed address", async () => {
    api(analyst, {});
    renderAt("/labels/owners/team/abc");
    expect(await screen.findByRole("heading", { name: /not found/i })).toBeInTheDocument();
  });
});
