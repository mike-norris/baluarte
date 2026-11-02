import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  Coverage,
  CoverageRow,
  DiscoveryContext,
  SecurityPolicyDetail,
  SecurityPolicySummary,
  TypeCoverage,
} from "../api/securityPolicies";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness, kind } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const LIST = "GET /api/v1/gitlab/security-policies?state=active&limit=100";
const COVERAGE = "GET /api/v1/gitlab/security-policies/coverage";
const DETAIL = "GET /api/v1/gitlab/security-policies/1";
const DISCOVER = "POST /api/v1/gitlab/security-policies/discover";
const SETTINGS = "GET /api/v1/settings";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:export", "evidence:read", "findings:read", "inventory:read"],
};
const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
};

const readable: DiscoveryContext = {
  readable: true,
  availability: {
    scan_execution: "ok",
    approval: "ok",
    pipeline_execution: "ok",
    vulnerability_management: "ok",
  },
  collection: kind("fresh"),
};

const SCAN_YAML = `---
name: Scan policy
enabled: true
rules:
- type: pipeline
  branch_type: default
actions:
- scan: secret_detection
- scan: sast
skip_ci:
  allowed: true
`;

function policy(
  id: number,
  name: string,
  extra: Partial<SecurityPolicySummary> = {},
): SecurityPolicySummary {
  return {
    id,
    policy_type: "scan_execution",
    name,
    description: null,
    enabled: true,
    origin_kind: "policy_project",
    origin_path: "corporate/security-policy-project",
    linked_from: [
      { type: "group", path: "facilities" },
      { type: "group", path: "finance" },
      { type: "group", path: "network" },
    ],
    state: "active",
    yaml_digest: "a".repeat(64),
    gitlab_updated_at: null,
    first_seen_at: "2026-10-31T18:06:39Z",
    last_seen_at: "2026-10-31T18:06:39Z",
    reaches: { groups: 3, projects: 1 },
    ...extra,
  };
}

const policies = [
  policy(1, "Scan policy"),
  policy(2, "Peer approval", { policy_type: "approval" }),
  policy(3, "License Approval Policy", {
    policy_type: "approval",
    origin_path: "acme/platform/security-policies",
    linked_from: [{ type: "group", path: "acme" }],
    reaches: { groups: 2, projects: 76 },
  }),
];

const detail: SecurityPolicyDetail = {
  ...policy(1, "Scan policy"),
  ...readable,
  yaml: SCAN_YAML,
  scope: {
    compliance_frameworks: [],
    including_projects: [],
    excluding_projects: ["finance/legacy"],
    including_groups: [],
    excluding_groups: [],
    excluding_archived_projects: null,
    excluding_personal_projects: null,
    truncated: false,
  },
  reached: {
    groups: [
      {
        id: 3,
        gitlab_id: 30,
        path: "finance",
        via: "finance",
        inherited: false,
        observation_id: 7,
        collected_at: "2026-10-31T18:06:39Z",
      },
    ],
    projects: [],
  },
};

function types(enabledScan: boolean, extra: Partial<TypeCoverage> = {}): CoverageRow["types"] {
  const none: TypeCoverage = {
    status: "ok",
    truncated: false,
    enabled: 0,
    disabled: 0,
    policies: [],
  };
  return {
    scan_execution: enabledScan
      ? {
          ...none,
          enabled: 1,
          policies: [
            { id: 1, name: "Scan policy", enabled: true, via: "finance", inherited: false },
          ],
        }
      : { ...none, ...extra },
    approval: none,
    pipeline_execution: none,
    vulnerability_management: none,
  };
}

function row(id: number, path: string, extra: Partial<CoverageRow> = {}): CoverageRow {
  return {
    id,
    gitlab_id: id * 10,
    path,
    status: "not_covered",
    reason: null,
    security_policy_project: null,
    observation: null,
    types: types(false),
    ...extra,
  };
}

const coverage: Coverage = {
  ...readable,
  summary: {
    groups: { covered: 1, not_covered: 2, unknown: 0 },
    projects: { covered: 1, not_covered: 2, unknown: 1 },
  },
  groups: [
    row(1, "corporate"),
    row(2, "data"),
    row(3, "finance", {
      status: "covered",
      security_policy_project: "corporate/security-policy-project",
      types: types(true),
    }),
  ],
  projects: [
    row(11, "corporate/artifacts"),
    row(12, "finance/ledger", { status: "covered", types: types(true) }),
    row(13, "finance/legacy"),
    row(14, "acme/new", {
      status: "unknown",
      reason: "security policies have not been read for this target",
    }),
  ],
};

function api(user: CurrentUser, extra: Record<string, (init?: RequestInit) => Response> = {}) {
  return mockApi({
    [ME]: () => json(user),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [LIST]: () => json({ ...readable, items: policies, next_after_id: null }),
    [COVERAGE]: () => json(coverage),
    [DETAIL]: () => json(detail),
    [SETTINGS]: () => json({ gitlab: { url: "https://gitlab.example.test" } }),
    ...extra,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function headerOf(call: Call | undefined, name: string): string | undefined {
  return (call?.init?.headers as Record<string, string> | undefined)?.[name];
}

describe("GitLab security policies", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists discovered policies read-only, separate from baselines", async () => {
    api(auditor);
    renderAt("/governance/gitlab-security-policies");

    const table = await screen.findByRole("table", { name: "GitLab security policies" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(
      within(rows[1] as HTMLElement).getByRole("link", { name: "Scan policy" }),
    ).toHaveAttribute("href", "/governance/gitlab-security-policies/1");
    expect(rows[1]).toHaveTextContent("Linked to facilities, finance, network");
    expect(rows[3]).toHaveTextContent("Merge request approval");
    expect(rows[3]).toHaveTextContent("2 groups, 76 projects");
    expect(screen.getByRole("link", { name: "GitLab security policies" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.queryByRole("button", { name: "Collect now" })).not.toBeInTheDocument();
  });

  it("lets an admin queue a collection now", async () => {
    const calls = api(admin, { [DISCOVER]: () => json({ job_id: 9 }, 202) });
    renderAt("/governance/gitlab-security-policies");

    await userEvent.click(await screen.findByRole("button", { name: "Collect now" }));

    expect(
      await screen.findByText("Queued. Results appear here when the collection finishes."),
    ).toBeInTheDocument();
    const post = calls.find((call) => call.init?.method === "POST");
    expect(post?.url).toBe("/api/v1/gitlab/security-policies/discover");
    expect(headerOf(post, "X-CSRF-Token")).toBe(admin.csrf_token);
  });

  it("never shows unreadable policies as none", async () => {
    api(auditor, {
      [LIST]: () =>
        json({
          readable: false,
          availability: {
            scan_execution: "unsupported",
            approval: "unsupported",
            pipeline_execution: "unsupported",
            vulnerability_management: "unsupported",
          },
          collection: kind("failed"),
          items: [],
          next_after_id: null,
        }),
    });
    renderAt("/governance/gitlab-security-policies");

    expect(
      await screen.findByText(/Baluarte couldn't read GitLab security policies: .*GitLab Ultimate/),
    ).toHaveTextContent("Coverage is unknown, not empty.");
    expect(
      screen.getByText(
        "None are listed because they couldn't be read; this doesn't mean there are none.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No GitLab security policies were found/)).not.toBeInTheDocument();
  });

  it("says when nothing has been collected yet", async () => {
    api(auditor, {
      [LIST]: () =>
        json({ ...readable, collection: kind("never"), items: [], next_after_id: null }),
    });
    renderAt("/governance/gitlab-security-policies");

    expect(
      await screen.findByText(
        "GitLab security policies haven't been collected yet. Coverage is unknown, not empty.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a policy's summary, notable settings, reach and verbatim YAML", async () => {
    api(admin);
    renderAt("/governance/gitlab-security-policies/1");

    expect(await screen.findByRole("heading", { name: "Scan policy" })).toBeInTheDocument();
    expect(screen.getByText("Runs secret detection and SAST.")).toBeInTheDocument();
    expect(screen.getByText("When: in pipelines on the default branch.")).toBeInTheDocument();
    expect(
      screen.getByText("skip_ci is allowed: anyone can skip this policy's jobs with [skip ci]."),
    ).toBeInTheDocument();
    expect(screen.getByText("Excludes projects: finance/legacy")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Groups (1)" })).toBeInTheDocument();
    expect(document.querySelector("pre code")).toHaveTextContent("name: Scan policy");
    expect(await screen.findByRole("link", { name: /Open in GitLab/ })).toHaveAttribute(
      "href",
      "https://gitlab.example.test/corporate/security-policy-project",
    );
    // Still on the GitLab security policies tab.
    expect(
      screen.getByRole("link", { name: "GitLab security policies", current: "page" }),
    ).toBeVisible();
  });

  it("leaves out the GitLab link for users who can't read settings", async () => {
    const calls = api(auditor);
    renderAt("/governance/gitlab-security-policies/1");

    expect(await screen.findByRole("heading", { name: "Scan policy" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open in GitLab/ })).not.toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/v1/settings")).toBe(false);
  });

  it("says an unknown policy isn't known", async () => {
    api(auditor, { [DETAIL]: () => json({ detail: "Not found" }, 404) });
    renderAt("/governance/gitlab-security-policies/1");

    expect(
      await screen.findByText(/This GitLab security policy isn't known to Baluarte/),
    ).toBeInTheDocument();
  });

  it("shows coverage gaps with the reason for each", async () => {
    api(auditor);
    renderAt("/governance/gitlab-security-policies/coverage");

    const groups = await screen.findByRole("heading", { name: "Groups" });
    expect(groups.parentElement).toHaveTextContent("1 covered · 2 not covered · 0 unknown");
    await userEvent.click(screen.getByRole("button", { name: "Only gaps" }));

    const table = screen.getByRole("table", { name: "GitLab security policy coverage by group" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((item) => within(item).getByRole("rowheader").textContent)).toEqual([
      "corporate",
      "data",
    ]);
    expect(rows[0]).toHaveTextContent(
      "No policy source project is linked to this group or the groups above it.",
    );
  });

  it("explains excluded and unknown projects, and never counts unknown as not covered", async () => {
    api(auditor);
    renderAt("/governance/gitlab-security-policies/coverage?kind=projects");

    const table = await screen.findByRole("table", {
      name: "GitLab security policy coverage by project",
    });
    const legacy = within(table).getByRole("row", { name: /finance\/legacy/ });
    expect(legacy).toHaveTextContent(
      "Its group finance is covered, but the policies' scope excludes this project.",
    );
    const unknown = within(table).getByRole("row", { name: /acme\/new/ });
    expect(unknown).toHaveTextContent("Unknown");
    expect(unknown).toHaveTextContent(
      "Unknown: security policies have not been read for this target. It isn't counted as covered or not covered.",
    );
    expect(unknown).not.toHaveTextContent("Not covered");

    await userEvent.click(screen.getByRole("button", { name: "Only gaps" }));
    expect(within(screen.getByRole("table")).queryByText("acme/new")).not.toBeInTheDocument();
  });

  it("mentions discovered policies on the Baselines first-run screen", async () => {
    api(analyst, {
      "GET /api/v1/policies": () => json([]),
      "GET /api/v1/policy-assignments?limit=100": () => json({ items: [], next_after_id: null }),
    });
    renderAt("/governance");

    expect(
      await screen.findByText(/We also found 3 GitLab security policies on this instance/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View them" })).toHaveAttribute(
      "href",
      "/governance/gitlab-security-policies",
    );
  });
});
