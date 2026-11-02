import { render, screen, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Evaluation } from "../api/evaluations";
import type { GroupDetail, ProjectDetail } from "../api/gitlab";
import type { Resolution, Target } from "../api/policies";
import type { Coverage, CoverageRow } from "../api/securityPolicies";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness, kind } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const PROJECT = "GET /api/v1/gitlab/projects/105";
const PROJECT_EFFECTIVE = "GET /api/v1/gitlab/projects/105/effective-policies";
const PROJECT_RESULTS = "GET /api/v1/gitlab/projects/105/evaluations";
const PROJECT_COVERAGE = "GET /api/v1/gitlab/security-policies/coverage?kind=project";
const GROUP = "GET /api/v1/gitlab/groups/42";
const GROUP_EFFECTIVE = "GET /api/v1/gitlab/groups/42/effective-policies";
const GROUP_COVERAGE = "GET /api/v1/gitlab/security-policies/coverage?kind=group";

const instance: Target = {
  target_type: "instance",
  target_id: 1,
  path: "https://gitlab.example.test",
  state: null,
};
const platform: Target = {
  target_type: "group",
  target_id: 42,
  path: "platform",
  state: "active",
};
const api105: Target = {
  target_type: "project",
  target_id: 105,
  path: "platform/api",
  state: "active",
};

const platformGroup = {
  id: 42,
  gitlab_id: 42,
  name: "platform",
  visibility: "private" as const,
  state: "active" as const,
  first_seen_at: "2026-10-30T20:00:00Z",
  last_seen_at: "2026-10-30T20:00:00Z",
  last_error: null,
  last_error_at: null,
  parent_gitlab_id: null,
  full_path: "platform",
};

const projectDetail: ProjectDetail = {
  project: {
    id: 105,
    gitlab_id: 105,
    name: "api",
    visibility: "internal",
    state: "active",
    first_seen_at: "2026-10-30T20:00:00Z",
    last_seen_at: "2026-10-30T20:00:00Z",
    last_error: null,
    last_error_at: null,
    namespace_gitlab_id: 42,
    namespace_kind: "group",
    path_with_namespace: "platform/api",
    archived: false,
    default_branch: "main",
  },
  ancestry: {
    groups: [{ gitlab_id: 42, full_path: "platform", state: "active" }],
    status: "complete",
    gaps: [],
  },
  collection: kind("fresh"),
  not_evaluated: null,
};

const groupDetail: GroupDetail = {
  group: platformGroup,
  ancestry: {
    groups: [],
    status: "incomplete",
    gaps: [{ gitlab_id: 7, reason: "inaccessible" }],
  },
  subgroups: 2,
  projects: 1,
  collection: kind("stale"),
};

function resolution(target: Target, path: Target[]): Resolution {
  return {
    target,
    ancestry: "complete",
    gaps: [],
    path,
    policies: [
      {
        assignment_id: 1,
        policy_id: 2,
        name: "starter-baseline",
        version: 2,
        yaml_digest: "2".repeat(64),
        assigned_to: instance,
        outcome: "effective",
      },
    ],
    considered: [],
  };
}

function evaluation(id: number, ruleId: string, extra: Partial<Evaluation> = {}): Evaluation {
  return {
    id,
    evaluation_run_id: 7,
    observation_id: 3,
    project: { gitlab_id: 105, path: "platform/api", state: "active" },
    policy: { policy_id: 2, name: "starter-baseline", version: 2 },
    rule_id: ruleId,
    rule_type: "gitlab.project.visibility",
    severity: "high",
    outcome: "pass",
    reason: "Visibility internal is allowed.",
    unknown_reasons: [],
    expected: ["private", "internal"],
    observed: "internal",
    source: "gitlab.projects",
    observed_at: "2026-10-30T20:00:00Z",
    capability: { name: null, status: "supported" },
    collection: {
      kind: "projects",
      status: "fresh",
      last_success_at: "2026-10-30T20:00:00Z",
    },
    ancestry: "complete",
    evaluated_at: "2026-10-30T20:10:00Z",
    exception_id: null,
    ...extra,
  };
}

function coverageRow(
  gitlabId: number,
  path: string,
  extra: Partial<CoverageRow> = {},
): CoverageRow {
  return {
    id: gitlabId,
    gitlab_id: gitlabId,
    path,
    status: "covered",
    reason: null,
    security_policy_project: null,
    observation: null,
    types: {
      scan_execution: {
        status: "ok",
        truncated: false,
        enabled: 1,
        disabled: 0,
        policies: [
          {
            id: 11,
            name: "Secret scanning",
            enabled: true,
            via: "corporate",
            inherited: true,
          },
        ],
      },
    },
    ...extra,
  };
}

function coverage(groups: CoverageRow[], projects: CoverageRow[], readable = true): Coverage {
  const counts = { covered: 0, not_covered: 0, unknown: 0 };
  return {
    readable,
    availability: { scan_execution: "ok", approval: "ok" },
    collection: kind("fresh"),
    summary: { groups: counts, projects: counts },
    groups,
    projects,
  };
}

function api(routes: Record<string, () => Response>) {
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
}

describe("a project's page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const projectRoutes = {
    [PROJECT]: () => json(projectDetail),
    [PROJECT_EFFECTIVE]: () => json(resolution(api105, [api105, platform, instance])),
    [PROJECT_RESULTS]: () =>
      json({
        project: { gitlab_id: 105, path: "platform/api", state: "active" },
        items: [
          evaluation(1, "project-visibility"),
          evaluation(2, "required-scans", {
            rule_type: "gitlab.security_policy.scan_execution",
            outcome: "unknown",
            reason: "Security policies could not be read.",
            unknown_reasons: ["security_policy_truncated"],
          }),
        ],
      }),
    [PROJECT_COVERAGE]: () => json(coverage([], [coverageRow(105, "platform/api")])),
  };

  it("shows what was collected, what applies and why, and its results", async () => {
    api(projectRoutes);
    renderAt("/inventory/projects/105");

    expect(await screen.findByRole("heading", { name: "platform/api" })).toBeInTheDocument();
    expect(
      within(screen.getByRole("navigation", { name: "Groups above this project" })).getByRole(
        "link",
        { name: "platform" },
      ),
    ).toHaveAttribute("href", "/inventory/groups/42");
    expect(screen.getByText("Internal")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();

    const inEffect = screen.getByRole("region", {
      name: "Baselines in effect",
    });
    expect(await within(inEffect).findByLabelText("Inheritance chain")).toHaveTextContent(
      "This instance ▸ platform ▸ platform/api",
    );
    expect(within(inEffect).getByRole("link", { name: "starter-baseline v2" })).toBeInTheDocument();

    const results = await screen.findByRole("table", {
      name: "Results for starter-baseline v2",
    });
    const unknown = within(results).getByRole("row", {
      name: /required-scans/,
    });
    expect(unknown).toHaveTextContent("Unknown");
    expect(unknown).toHaveTextContent("GitLab listed more security policies of this type");
    expect(
      within(unknown).getByRole("link", {
        name: "Findings for required-scans from starter-baseline",
      }),
    ).toHaveAttribute("href", "/findings?project=105&policy=starter-baseline&rule=required-scans");
    const passing = within(results).getByRole("row", {
      name: /project-visibility/,
    });
    expect(passing).toHaveTextContent("Pass");
    expect(within(passing).queryByRole("link")).not.toBeInTheDocument();

    const policies = screen.getByRole("region", {
      name: "GitLab security policies reaching this",
    });
    expect(await within(policies).findByText("Covered")).toBeInTheDocument();
    expect(within(policies).getByRole("link", { name: "Secret scanning" })).toHaveAttribute(
      "href",
      "/governance/gitlab-security-policies/11",
    );
    expect(policies).toHaveTextContent("Scan execution, enabled, from corporate (inherited)");
  });

  it("says when it has no current results", async () => {
    api({
      ...projectRoutes,
      [PROJECT_RESULTS]: () => json({ project: projectDetail.project, items: [] }),
    });
    renderAt("/inventory/projects/105");

    expect(await screen.findByText(/No current results/)).toBeInTheDocument();
  });

  it("says an archived project is not checked by baselines", async () => {
    const calls = api({
      ...projectRoutes,
      [PROJECT]: () =>
        json({
          ...projectDetail,
          project: { ...projectDetail.project, archived: true },
          not_evaluated: "archived",
        }),
    });
    renderAt("/inventory/projects/105");

    const results = await screen.findByRole("region", { name: "Latest results" });
    expect(results).toHaveTextContent(
      "Archived: not checked by baselines. Findings were resolved when it was archived.",
    );
    expect(screen.getByTitle("Archived projects are not checked by baselines.")).toHaveTextContent(
      "Yes",
    );
    expect(calls.map((call) => call.url)).not.toContain("/api/v1/gitlab/projects/105/evaluations");
  });

  it("says coverage is unknown when GitLab security policies can't be read", async () => {
    api({
      ...projectRoutes,
      [PROJECT_COVERAGE]: () => json(coverage([], [], false)),
    });
    renderAt("/inventory/projects/105");

    const policies = await screen.findByRole("region", {
      name: "GitLab security policies reaching this",
    });
    expect(await within(policies).findByText(/Coverage is unknown, not empty/)).toBeInTheDocument();
  });

  it("says when the project isn't in the inventory", async () => {
    api({ [PROJECT]: () => json({ detail: "Not found" }, 404) });
    renderAt("/inventory/projects/105");

    expect(await screen.findByRole("heading", { name: "Project not found" })).toBeInTheDocument();
  });
});

describe("a group's page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows its baselines, an unfollowable parent and the policies reaching it", async () => {
    api({
      [GROUP]: () => json(groupDetail),
      [GROUP_EFFECTIVE]: () => json(resolution(platform, [platform, instance])),
      [GROUP_COVERAGE]: () =>
        json(coverage([coverageRow(42, "platform", { status: "not_covered", types: {} })], [])),
    });
    renderAt("/inventory/groups/42");

    expect(await screen.findByRole("heading", { name: "platform" })).toBeInTheDocument();
    expect(screen.getByText(/2 subgroups, 1 project directly/)).toBeInTheDocument();
    expect(screen.getByText(/group 7 is not readable with Baluarte's token/)).toBeInTheDocument();
    expect(screen.getByText(/Stale/)).toBeInTheDocument();
    const inEffect = screen.getByRole("region", {
      name: "Baselines in effect",
    });
    expect(await within(inEffect).findByText(/— from This instance/)).toBeInTheDocument();
    const policies = screen.getByRole("region", {
      name: "GitLab security policies reaching this",
    });
    expect(await within(policies).findByText("Not covered")).toBeInTheDocument();
    expect(policies).toHaveTextContent("No policy source project is linked to this group");
  });
});
