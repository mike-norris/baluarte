import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PracticeSettings, ProjectList, ReleasePractice } from "../api/releasePractices";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { settingsLines } from "./ReleasePracticesPage";

const ME = "GET /api/v1/auth/me";
const LIST = "GET /api/v1/release-practices";
const ASSIGNMENTS = "GET /api/v1/release-practice-assignments";
const PROJECTS = "GET /api/v1/release-practices/projects";
const CREATE = "POST /api/v1/release-practices";

const tagDefaults: PracticeSettings = {
  production_from: "tag",
  ref_patterns: ["*-release"],
  environments: ["production"],
  required_approvals: 1,
  creator_may_approve: false,
  deployer_may_approve: false,
  require_merge_request_review: true,
};
const presets = [
  {
    preset: "merge_request",
    label: "Merge request to the default branch",
    settings: {
      ...tagDefaults,
      production_from: "default_branch",
      ref_patterns: [],
      required_approvals: 0,
    },
  },
  {
    preset: "release_tag",
    label: "Protected release tag with deployment approval",
    settings: tagDefaults,
  },
  {
    preset: "release_branch",
    label: "Release branch",
    settings: {
      ...tagDefaults,
      production_from: "branch",
      ref_patterns: ["release/*"],
      required_approvals: 0,
    },
  },
  { preset: "custom", label: "Custom", settings: null },
];
const practice: ReleasePractice = {
  name: "tag-releases",
  preset: "release_tag",
  preset_label: "Protected release tag with deployment approval",
  description: "Releases from tags",
  settings: { ...tagDefaults, ref_patterns: ["*-release", "20*-release"] },
  assignments: 1,
  created_by: "user-1",
  created_at: "2026-11-25T10:00:00Z",
  updated_by: "user-1",
  updated_at: "2026-11-25T10:00:00Z",
};
const projects: ProjectList = {
  window_days: 90,
  counts: { follows: 1, differs: 1 },
  projects: [
    {
      project_id: 1,
      gitlab_id: 100,
      path: "platform/api",
      default_branch: "main",
      practice: "tag-releases",
      preset: "release_tag",
      decided_by: { target_type: "group", target_id: 10, path: "platform", state: "active" },
      status: "follows",
      observed: {
        deployments: 3,
        by_kind: { tag: 3 },
        expected: 3,
        other: 0,
        unknown: 0,
        other_refs: [],
        last_deployed_at: "2026-11-24T10:00:00Z",
      },
    },
    {
      project_id: 2,
      gitlab_id: 101,
      path: "platform/web",
      default_branch: "main",
      practice: "tag-releases",
      preset: "release_tag",
      decided_by: { target_type: "group", target_id: 10, path: "platform", state: "active" },
      status: "differs",
      observed: {
        deployments: 2,
        by_kind: { default_branch: 1, tag: 1 },
        expected: 1,
        other: 1,
        unknown: 0,
        other_refs: ["main"],
        last_deployed_at: "2026-11-24T10:00:00Z",
      },
    },
  ],
};

function api(user: CurrentUser, extra: Record<string, (init?: RequestInit) => Response> = {}) {
  return mockApi({
    [ME]: () => json(user),
    [LIST]: () => json({ presets, practices: [practice], window_days: 90 }),
    [ASSIGNMENTS]: () =>
      json({
        assignments: [
          {
            id: 4,
            connection_id: 1,
            name: "tag-releases",
            target: { target_type: "group", target_id: 10, path: "platform", state: "active" },
            assigned_by: "user-1",
            created_at: "2026-11-25T10:00:00Z",
          },
        ],
      }),
    [PROJECTS]: () => json(projects),
    ...extra,
  });
}

function renderPage() {
  const router = createMemoryRouter(routes, {
    initialEntries: ["/governance/release-practices"],
  });
  render(<RouterProvider router={router} />);
}

function bodyOf(calls: Call[], key: string): unknown {
  const call = calls.find((c) => `${c.init?.method ?? "GET"} ${c.url}` === key);
  return JSON.parse(String(call?.init?.body));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("settingsLines", () => {
  it("describes a tag practice with its separation of duties", () => {
    expect(settingsLines(practice.settings)).toEqual([
      "Production deploys tags matching *-release, 20*-release.",
      "Environments: production.",
      "1 deployment approval required.",
      "Whoever started the release (for a tag, its creator) may not approve it.",
      "The deployer may not be an approver.",
      "Each change to the default branch is reviewed by someone other than its author.",
    ]);
  });
});

describe("ReleasePracticesPage", () => {
  it("shows practices, where they apply and what releases show", async () => {
    api(analyst);
    renderPage();

    const card = await screen.findByRole("article", { name: "tag-releases" });
    expect(within(card).getByText("Assigned in 1 place")).toBeInTheDocument();
    expect(within(card).getByText("The deployer may not be an approver.")).toBeInTheDocument();
    expect(await screen.findByRole("rowheader", { name: "Group platform" })).toBeInTheDocument();

    const table = await screen.findByRole("table", {
      name: "Projects and their release practices",
    });
    const web = within(table).getByRole("row", { name: /platform\/web/ });
    expect(within(web).getByText("Differs")).toBeInTheDocument();
    expect(within(web).getByText("Not what the practice expects: main")).toBeInTheDocument();
    expect(within(web).getByText("From group platform")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Show"), "follows");
    expect(within(table).queryByRole("row", { name: /platform\/web/ })).not.toBeInTheDocument();
  });

  it("creates a practice from a preset with its parameters", async () => {
    const calls = api(analyst, {
      [CREATE]: () => json({ ...practice, name: "branches" }, 201),
    });
    renderPage();

    await userEvent.click(await screen.findByRole("radio", { name: "Release branch" }));
    await userEvent.type(screen.getByLabelText("Name"), "branches");
    const patterns = screen.getByLabelText(/Branches matching/);
    await userEvent.clear(patterns);
    await userEvent.type(patterns, "release/*, hotfix/*");
    await userEvent.click(screen.getByRole("button", { name: "Add practice" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Created the release practice branches.",
    );
    expect(bodyOf(calls, CREATE)).toEqual({
      name: "branches",
      preset: "release_branch",
      description: null,
      settings: {
        ref_patterns: ["release/*", "hotfix/*"],
        environments: ["production"],
        required_approvals: 0,
        creator_may_approve: false,
        deployer_may_approve: false,
        require_merge_request_review: true,
      },
    });
  });

  it("lets readers look without changing anything", async () => {
    api({ ...analyst, roles: ["auditor"], permissions: ["findings:read", "evidence:read"] });
    renderPage();

    await screen.findByRole("article", { name: "tag-releases" });
    expect(screen.queryByRole("button", { name: "Add practice" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit tag-releases" })).not.toBeInTheDocument();
  });
});
