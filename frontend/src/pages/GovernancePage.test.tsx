import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Group, Project } from "../api/gitlab";
import type {
  Assignment,
  AssignmentTree,
  PolicyVersion,
  Resolution,
  Target,
  TreeEffect,
} from "../api/policies";
import type { CurrentUser } from "../auth/session";
import { recommendedBaseline, toDocument, toYaml } from "../policies/yaml";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import type { EvaluationSummary } from "../api/evaluations";
import {
  EVALUATION_FRESHNESS,
  evaluationFreshness,
  evaluationRun,
  freshness,
} from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const POLICIES = "GET /api/v1/policies";
const ASSIGNMENTS = "GET /api/v1/policy-assignments?limit=100";
const BASELINE_ASSIGNMENTS =
  "GET /api/v1/policy-assignments?policy_name=starter-baseline&limit=100";
const VERSIONS = "GET /api/v1/policies/starter-baseline/versions";
const VERSION_1 = "GET /api/v1/policies/starter-baseline/versions/1";
const VERSION_2 = "GET /api/v1/policies/starter-baseline/versions/2";
const SAVE = "POST /api/v1/policies";
const VALIDATE = "POST /api/v1/policies/validate";
const ASSIGN = "POST /api/v1/policy-assignments";
const TREE = "GET /api/v1/policy-assignments/tree";
const SUMMARY = "GET /api/v1/evaluations/summary";
const BASELINE_SUMMARY = "GET /api/v1/evaluations/summary?policy=starter-baseline";
const GROUP_SEARCH = "GET /api/v1/gitlab/groups?search=platform&state=active&limit=100";
const PROJECT_SEARCH = "GET /api/v1/gitlab/projects?search=api&state=active&limit=100";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:export", "evidence:read", "findings:read", "inventory:read"],
};

const summary = {
  name: "starter-baseline",
  latest_version: 2,
  versions: 2,
  latest_effective_at: "2026-10-30T20:00:00Z",
};

function versionSummary(version: number, name = "starter-baseline") {
  return {
    id: version,
    name,
    version,
    yaml_digest: String(version).repeat(64),
    effective_at: "2026-10-30T20:00:00Z",
    created_at: "2026-10-30T20:00:00Z",
  };
}

function policyVersion(version: number, name = "starter-baseline"): PolicyVersion {
  const form = { ...recommendedBaseline(), name };
  return {
    ...versionSummary(version, name),
    source_yaml: toYaml(form),
    document: toDocument(form),
  };
}

function assignment(id: number, extra: Partial<Assignment> = {}): Assignment {
  return {
    id,
    policy_id: 2,
    policy_name: "starter-baseline",
    version: 2,
    yaml_digest: "2".repeat(64),
    target_type: "instance",
    target_id: 1,
    target_path: "https://gitlab.example.test",
    target_state: null,
    created_at: "2026-10-30T20:05:00Z",
    ...extra,
  };
}

function group(gitlabId: number, path: string, parent: number | null = null): Group {
  return {
    id: gitlabId,
    gitlab_id: gitlabId,
    name: path.split("/").pop() ?? path,
    visibility: "private",
    state: "active",
    first_seen_at: "2026-10-30T20:00:00Z",
    last_seen_at: "2026-10-30T20:00:00Z",
    last_error: null,
    last_error_at: null,
    parent_gitlab_id: parent,
    full_path: path,
  };
}

function project(gitlabId: number, path: string): Project {
  return {
    id: gitlabId,
    gitlab_id: gitlabId,
    name: path.split("/").pop() ?? path,
    visibility: "public",
    state: "active",
    first_seen_at: "2026-10-30T20:00:00Z",
    last_seen_at: "2026-10-30T20:00:00Z",
    last_error: null,
    last_error_at: null,
    namespace_gitlab_id: 7,
    namespace_kind: "group",
    path_with_namespace: path,
    archived: false,
    default_branch: "main",
  };
}

function evaluationSummary(baselines: EvaluationSummary["baselines"] = []): EvaluationSummary {
  return {
    evaluation_run: baselines.length > 0 ? evaluationRun(7) : null,
    freshness: evaluationFreshness(),
    baselines,
  };
}

const counts = (pass: number, fail: number, unknown = 0) => ({
  pass,
  fail,
  unknown,
  unsupported: 0,
  excepted: 0,
});

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, routes: Routes = {}): Call[] {
  return mockApi({
    [ME]: () => json(user),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [POLICIES]: () => json([summary]),
    [ASSIGNMENTS]: () => json({ items: [assignment(1)], next_after_id: null }),
    [BASELINE_ASSIGNMENTS]: () => json({ items: [assignment(1)], next_after_id: null }),
    [VERSIONS]: () => json([versionSummary(2), versionSummary(1)]),
    [VERSION_1]: () => json(policyVersion(1)),
    [VERSION_2]: () => json(policyVersion(2)),
    [SUMMARY]: () => json(evaluationSummary()),
    [BASELINE_SUMMARY]: () => json(evaluationSummary()),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function bodyOf(call: Call | undefined): unknown {
  return JSON.parse(String(call?.init?.body));
}

function headerOf(call: Call | undefined, name: string): string | undefined {
  return (call?.init?.headers as Record<string, string> | undefined)?.[name];
}

function calledWith(calls: Call[], key: string): Call[] {
  return calls.filter((call) => `${call.init?.method ?? "GET"} ${call.url}` === key);
}

describe("governance: baselines", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists baselines and where they apply, read-only without policy:write", async () => {
    api(auditor);
    renderAt("/governance");

    const table = await screen.findByRole("table", { name: "Baselines" });
    const row = within(table).getByRole("row", { name: /starter-baseline/ });
    expect(within(row).getByRole("link", { name: "starter-baseline" })).toHaveAttribute(
      "href",
      "/governance/baselines/starter-baseline",
    );
    expect(row).toHaveTextContent("Whole instance (v2)");
    expect(within(row).getByRole("link", { name: "Open findings" })).toHaveAttribute(
      "href",
      "/findings?state=open&policy=starter-baseline",
    );
    expect(screen.getByRole("navigation", { name: "Governance views" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a baseline" })).not.toBeInTheDocument();
  });

  it("counts projects passing, failing and unknown from the latest evaluation", async () => {
    api(auditor, {
      [SUMMARY]: () =>
        json(
          evaluationSummary([
            {
              name: "starter-baseline",
              projects: counts(95, 3, 2),
              versions: [],
            },
          ]),
        ),
    });
    renderAt("/governance");

    const table = await screen.findByRole("table", { name: "Baselines" });
    const row = within(table).getByRole("row", { name: /starter-baseline/ });
    await vi.waitFor(() => {
      expect(row).toHaveTextContent("100 projects:3 failing·95 passing·2 unknown");
    });
    expect(within(row).getByRole("link", { name: "3 failing" })).toHaveAttribute(
      "href",
      "/findings?state=open&policy=starter-baseline",
    );
    expect(screen.getByText(/Results from the evaluation finished/)).toBeInTheDocument();
  });

  it("offers the recommended baseline when nothing is checked yet", async () => {
    api(analyst, {
      [POLICIES]: () => json([]),
      [ASSIGNMENTS]: () => json({ items: [], next_after_id: null }),
    });
    renderAt("/governance");

    expect(
      await screen.findByRole("heading", { name: "Baluarte isn't checking anything yet" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Projects are not public")).toBeInTheDocument();
    expect(screen.getByText("Every default branch is protected")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Use the recommended baseline" })).toHaveAttribute(
      "href",
      "/governance/baselines/new?start=recommended",
    );
  });

  it("tells read-only users who can create the first baseline", async () => {
    api(auditor, {
      [POLICIES]: () => json([]),
      [ASSIGNMENTS]: () => json({ items: [], next_after_id: null }),
    });
    renderAt("/governance");

    expect(
      await screen.findByText("No baselines yet. An administrator or analyst can create one."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /recommended/ })).not.toBeInTheDocument();
  });

  it("warns when baselines exist but none is applied", async () => {
    api(analyst, { [ASSIGNMENTS]: () => json({ items: [], next_after_id: null }) });
    renderAt("/governance");

    expect(
      await screen.findByText(/No baseline is applied anywhere, so nothing is checked/),
    ).toBeInTheDocument();
  });

  it("redirects the old Policies links", async () => {
    api(auditor);
    const router = renderAt("/policies/starter-baseline?version=1");

    expect(await screen.findByRole("heading", { name: "starter-baseline" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/governance/baselines/starter-baseline");
    expect(router.state.location.search).toBe("?version=1");
  });
});

describe("governance: the baseline editor", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("saves the recommended baseline and applies it to the whole instance in one step", async () => {
    const saved = policyVersion(1, "recommended-baseline");
    const calls = api(analyst, {
      [SAVE]: () => json(saved, 201),
      [ASSIGN]: () => json(assignment(9, { policy_name: "recommended-baseline", version: 1 }), 201),
      "GET /api/v1/policies/recommended-baseline/versions": () =>
        json([versionSummary(1, "recommended-baseline")]),
      "GET /api/v1/policies/recommended-baseline/versions/1": () => json(saved),
      "GET /api/v1/policy-assignments?policy_name=recommended-baseline&limit=100": () =>
        json({
          items: [assignment(9, { policy_name: "recommended-baseline", version: 1 })],
          next_after_id: null,
        }),
    });
    const user = userEvent.setup();
    const router = renderAt("/governance/baselines/new?start=recommended");

    expect(
      await screen.findByRole("heading", { name: "Use the recommended baseline" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Private" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Internal" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Public" })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: "Whole instance" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save and apply" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Saved version 1 of recommended-baseline. Applied recommended-baseline v1 to the whole instance. Checking now",
    );
    expect(router.state.location.pathname).toBe("/governance/baselines/recommended-baseline");
    const save = calledWith(calls, SAVE)[0];
    expect(headerOf(save, "X-CSRF-Token")).toBe(analyst.csrf_token);
    expect(bodyOf(save)).toEqual({ source_yaml: toYaml(recommendedBaseline()) });
    expect(bodyOf(calledWith(calls, ASSIGN)[0])).toEqual({
      policy_name: "recommended-baseline",
      version: 1,
      target_type: "instance",
    });
  });

  it("proposes the baseline for review instead of applying it when review is required", async () => {
    const calls = api(analyst, {
      [SAVE]: () =>
        json(
          {
            id: 3,
            kind: "version",
            state: "proposed",
            policy_name: "recommended-baseline",
            version: null,
            yaml_digest: "a".repeat(64),
            base_version: null,
            target_type: null,
            target_id: null,
            target_path: null,
            assignment_id: null,
            review_required: true,
            self_approved: false,
            migrated: false,
            proposed_by: { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" },
            proposed_at: "2026-11-01T10:00:00Z",
            decided_by: null,
            decided_at: null,
            decision_note: null,
            source_yaml: "x",
            base_source_yaml: null,
          },
          202,
        ),
    });
    const user = userEvent.setup();
    const router = renderAt("/governance/baselines/new?start=recommended");

    await user.click(await screen.findByRole("button", { name: "Save and apply" }));

    expect(
      await screen.findByText(
        "Proposed: New baseline recommended-baseline. It takes effect when someone else approves it under Governance → Changes. Apply it once it is approved.",
      ),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/governance/changes");
    expect(calledWith(calls, ASSIGN)).toEqual([]);
  });

  it("says nothing changed when the version was already applied there", async () => {
    const saved = policyVersion(1, "recommended-baseline");
    api(analyst, {
      [SAVE]: () => json(saved, 200),
      [ASSIGN]: () => json(assignment(9, { policy_name: "recommended-baseline", version: 1 }), 200),
      "GET /api/v1/policies/recommended-baseline/versions": () =>
        json([versionSummary(1, "recommended-baseline")]),
      "GET /api/v1/policies/recommended-baseline/versions/1": () => json(saved),
      "GET /api/v1/policy-assignments?policy_name=recommended-baseline&limit=100": () =>
        json({ items: [], next_after_id: null }),
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/new?start=recommended");

    await user.click(await screen.findByRole("button", { name: "Save and apply" }));

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("identical to version 1");
    expect(status).toHaveTextContent(
      "recommended-baseline v1 was already applied to the whole instance. Nothing changed.",
    );
  });

  it("checks the form before saving", async () => {
    const calls = api(analyst);
    const user = userEvent.setup();
    renderAt("/governance/baselines/new");

    await user.click(await screen.findByRole("button", { name: "Save without applying" }));

    expect(await screen.findByText("Fix these before saving:")).toBeInTheDocument();
    expect(screen.getByText("Rules: turn on at least one rule.")).toBeInTheDocument();
    expect(calledWith(calls, SAVE)).toHaveLength(0);
  });

  it("adds the GitLab security policy rules only when asked", async () => {
    const calls = api(analyst, { [SAVE]: () => json(policyVersion(3), 201) });
    const user = userEvent.setup();
    renderAt("/governance/baselines/new?start=recommended");

    await screen.findByRole("heading", {
      name: "Use the recommended baseline",
    });
    expect(screen.queryByRole("region", { name: "Required scans rule" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add rule: required scans" }));
    await user.click(screen.getByRole("button", { name: "Add rule: required approvals" }));

    const scansRule = screen.getByRole("region", {
      name: "Required scans rule",
    });
    expect(within(scansRule).getByRole("checkbox", { name: "Secret detection" })).toBeChecked();
    await user.click(within(scansRule).getByRole("checkbox", { name: "Dependency scanning" }));
    await user.click(
      within(scansRule).getByRole("checkbox", {
        name: "Count a policy that lets pipelines skip CI",
      }),
    );
    const approvalsRule = screen.getByRole("region", {
      name: "Required approvals rule",
    });
    const count = within(approvalsRule).getByRole("spinbutton", {
      name: "Approvals required, at least",
    });
    await user.clear(count);
    await user.type(count, "2");
    await user.click(
      within(approvalsRule).getByRole("checkbox", {
        name: "The policy must stop the author approving",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Save without applying" }));

    await vi.waitFor(() => {
      expect(calledWith(calls, SAVE)).toHaveLength(1);
    });
    const yaml = (bodyOf(calledWith(calls, SAVE)[0]) as { source_yaml: string }).source_yaml;
    expect(yaml).toContain(`      type: gitlab.security_policy.scan_execution
      scans: [secret_detection, sast, dependency_scanning]
      allow_skip_ci: false`);
    expect(yaml).toContain(`      type: gitlab.security_policy.approval
      min_approvals: 2
      forbid_author_approval: true
      forbid_committer_approval: false`);
  });

  it("removes an added rule", async () => {
    api(analyst);
    const user = userEvent.setup();
    renderAt("/governance/baselines/new");

    await user.click(
      await screen.findByRole("button", {
        name: "Add rule: required approvals",
      }),
    );
    const rule = screen.getByRole("region", {
      name: "Required approvals rule",
    });
    await user.click(within(rule).getByRole("button", { name: "Remove rule" }));
    expect(
      screen.queryByRole("region", { name: "Required approvals rule" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Add rule: required approvals" }),
    ).toBeInTheDocument();
  });

  it("applies a new baseline to a group found by path", async () => {
    const saved = policyVersion(1, "platform-rules");
    const calls = api(analyst, {
      [GROUP_SEARCH]: () =>
        json({
          items: [group(42, "platform"), group(43, "platform/api", 42)],
          next_after_id: null,
        }),
      [SAVE]: () => json(saved, 201),
      [ASSIGN]: () =>
        json(
          assignment(9, {
            policy_name: "platform-rules",
            version: 1,
            target_type: "group",
            target_id: 42,
            target_path: "platform",
          }),
          201,
        ),
      "GET /api/v1/policies/platform-rules/versions": () =>
        json([versionSummary(1, "platform-rules")]),
      "GET /api/v1/policies/platform-rules/versions/1": () => json(saved),
      "GET /api/v1/policy-assignments?policy_name=platform-rules&limit=100": () =>
        json({ items: [], next_after_id: null }),
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/new");

    await user.type(await screen.findByRole("textbox", { name: "Name" }), "platform-rules");
    await user.click(screen.getByRole("checkbox", { name: "Every default branch is protected" }));
    await user.click(screen.getByRole("radio", { name: "A group" }));
    await user.type(screen.getByRole("searchbox", { name: "Find a group by path" }), "platform");
    await user.click(screen.getByRole("button", { name: "Search groups" }));
    await user.click(await screen.findByRole("radio", { name: /^platform\s?private$/ }));
    await user.click(screen.getByRole("button", { name: "Save and apply" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Applied platform-rules v1 to group platform.",
    );
    expect(bodyOf(calledWith(calls, ASSIGN)[0])).toEqual({
      policy_name: "platform-rules",
      version: 1,
      target_type: "group",
      target_id: 42,
    });
    const source = (bodyOf(calledWith(calls, SAVE)[0]) as { source_yaml: string }).source_yaml;
    expect(source).toContain("name: platform-rules");
    expect(source).toContain("gitlab.branch.default_protected");
    expect(source).not.toContain("gitlab.project.visibility");
  });

  it("edits as YAML and lists validation errors with their paths", async () => {
    api(analyst, {
      [VALIDATE]: () =>
        json({
          valid: false,
          errors: [{ path: "spec.rules[0].allowed", message: "List should have at least 1 item" }],
          name: null,
          yaml_digest: null,
          rule_ids: [],
        }),
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/new?mode=yaml");

    expect(
      await screen.findByRole("heading", { name: "Import a baseline as YAML" }),
    ).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Baseline YAML" }), "kind: GitLabBaseline");
    await user.click(screen.getByRole("button", { name: "Check YAML" }));

    expect(await screen.findByText("Not valid: 1 problem.")).toBeInTheDocument();
    expect(screen.getByText("spec.rules[0].allowed")).toBeInTheDocument();
  });

  it("shows the form's YAML when switching to the advanced editor", async () => {
    api(analyst);
    const user = userEvent.setup();
    renderAt("/governance/baselines/new?start=recommended");

    await user.click(await screen.findByRole("button", { name: "Edit as YAML (advanced)" }));

    expect(screen.getByRole("textbox", { name: "Baseline YAML" })).toHaveValue(
      toYaml(recommendedBaseline()),
    );
    expect(
      screen.getByText("Going back to the form discards changes made here."),
    ).toBeInTheDocument();
  });

  it("makes a new version of an existing baseline with its name fixed", async () => {
    const calls = api(analyst, {
      [SAVE]: () => json(policyVersion(3), 201),
      "GET /api/v1/policies/starter-baseline/versions/3": () => json(policyVersion(3)),
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/new?from=starter-baseline&version=2");

    const name = await screen.findByRole("textbox", { name: "Name" });
    expect(name).toHaveValue("starter-baseline");
    expect(name).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: "Save and apply" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Public" }));
    await user.click(screen.getByRole("button", { name: "Save new version" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Saved version 3 of starter-baseline.",
    );
    const source = (bodyOf(calledWith(calls, SAVE)[0]) as { source_yaml: string }).source_yaml;
    expect(source).toContain("allowed: [private, internal, public]");
    expect(calledWith(calls, ASSIGN)).toHaveLength(0);
  });

  it("opens a version the form can't show as YAML", async () => {
    api(analyst, {
      [VERSION_2]: () => json({ ...policyVersion(2), source_yaml: "custom: yaml\n", document: {} }),
    });
    renderAt("/governance/baselines/new?from=starter-baseline&version=2");

    expect(await screen.findByRole("textbox", { name: "Baseline YAML" })).toHaveValue(
      "custom: yaml\n",
    );
    expect(
      screen.getByText("This version uses settings the form can't show, so it opens as YAML."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Form" })).toBeDisabled();
  });

  it("refuses the editor to users without policy:write", async () => {
    api(auditor);
    renderAt("/policy-versions/new");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You do not have permission to change baselines.",
    );
  });
});

describe("governance: a baseline", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows its rules as sentences, where it is applied, and its versions", async () => {
    api(auditor);
    renderAt("/governance/baselines/starter-baseline");

    expect(await screen.findByRole("heading", { name: "Rules in version 2" })).toBeInTheDocument();
    expect(screen.getByText("Projects are not public")).toBeInTheDocument();
    expect(screen.getByText("Allowed: private, internal.")).toBeInTheDocument();
    expect(
      screen.getByText("Covers every group and project it is applied to."),
    ).toBeInTheDocument();
    const applied = await screen.findByRole("table", { name: "Where this baseline is applied" });
    expect(within(applied).getByRole("rowheader")).toHaveTextContent("Whole instance");
    expect(screen.getByRole("link", { name: "v2 (latest)" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.queryByRole("button", { name: "Apply…" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
  });

  it("counts results for the version and for each rule", async () => {
    api(auditor, {
      [BASELINE_SUMMARY]: () =>
        json(
          evaluationSummary([
            {
              name: "starter-baseline",
              projects: counts(4, 1),
              versions: [
                {
                  policy_id: 2,
                  version: 2,
                  yaml_digest: "2".repeat(64),
                  projects: counts(4, 1),
                  rules: [
                    {
                      rule_id: "project-visibility",
                      rule_type: "gitlab.project.visibility",
                      severity: "high",
                      results: counts(4, 1),
                    },
                    {
                      rule_id: "protected-default-branch",
                      rule_type: "gitlab.branch.default_protected",
                      severity: "medium",
                      results: counts(3, 0, 2),
                    },
                  ],
                },
              ],
            },
          ]),
        ),
    });
    renderAt("/governance/baselines/starter-baseline");

    expect(await screen.findByText("5 projects checked:")).toBeInTheDocument();
    const visibility = await screen.findByLabelText("Results for project-visibility");
    expect(visibility).toHaveTextContent("1 failing·4 passing");
    expect(within(visibility).getByRole("link", { name: "1 failing" })).toHaveAttribute(
      "href",
      "/findings?state=open&policy=starter-baseline&rule=project-visibility",
    );
    expect(screen.getByLabelText("Results for protected-default-branch")).toHaveTextContent(
      "3 passing·2 unknown",
    );
  });

  it("rolls out the latest version to places still on an older one", async () => {
    const calls = api(analyst, {
      [BASELINE_ASSIGNMENTS]: () =>
        json({
          items: [
            assignment(1, { version: 1 }),
            assignment(2, {
              version: 1,
              target_type: "group",
              target_id: 42,
              target_path: "platform",
            }),
          ],
          next_after_id: null,
        }),
      [ASSIGN]: (init) => {
        const body = JSON.parse(String(init?.body)) as { target_type: string };
        return json(
          body.target_type === "group"
            ? assignment(4, { target_type: "group", target_id: 42, target_path: "platform" })
            : assignment(3),
          201,
        );
      },
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/starter-baseline");

    expect(
      await screen.findByText("v2 is newer than the version applied in 2 places."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Review and apply v2…" }));
    expect(
      screen.getByText("Apply v2 to: the whole instance; group platform?"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply v2" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Applied starter-baseline v2 to the whole instance.",
    );
    expect(calledWith(calls, ASSIGN).map(bodyOf)).toEqual([
      { policy_name: "starter-baseline", version: 2, target_type: "instance" },
      { policy_name: "starter-baseline", version: 2, target_type: "group", target_id: 42 },
    ]);
  });

  it("applies a chosen version to a project and shows the API's reason when refused", async () => {
    const calls = api(analyst, {
      [PROJECT_SEARCH]: () => json({ items: [project(7, "platform/api")], next_after_id: null }),
      [ASSIGN]: () => json({ detail: "The target is not active in the inventory" }, 409),
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/starter-baseline");

    await user.click(await screen.findByRole("button", { name: "Apply…" }));
    const form = screen.getByRole("form", { name: "Apply starter-baseline" });
    await user.selectOptions(within(form).getByRole("combobox", { name: "Version" }), "1");
    await user.click(within(form).getByRole("radio", { name: "A project" }));
    await user.type(within(form).getByRole("searchbox", { name: "Find a project by path" }), "api");
    await user.click(within(form).getByRole("button", { name: "Search projects" }));
    await user.click(await within(form).findByRole("radio", { name: /platform\/api/ }));
    await user.click(within(form).getByRole("button", { name: "Apply" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Not possible now: The target is not active in the inventory.",
    );
    expect(bodyOf(calledWith(calls, ASSIGN)[0])).toEqual({
      policy_name: "starter-baseline",
      version: 1,
      target_type: "project",
      target_id: 7,
    });
  });

  it("stops checking it on a target after confirming", async () => {
    let removed = false;
    const calls = api(analyst, {
      [BASELINE_ASSIGNMENTS]: () =>
        json({ items: removed ? [] : [assignment(1)], next_after_id: null }),
      "DELETE /api/v1/policy-assignments/1": () => {
        removed = true;
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/starter-baseline");

    await user.click(await screen.findByRole("button", { name: "Remove from Whole instance" }));
    expect(
      screen.getByText(/Stop checking starter-baseline on the whole instance\?/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Stopped checking starter-baseline on the whole instance.",
    );
    expect(
      await screen.findByText("Not applied anywhere yet, so nothing is checked against it."),
    ).toBeInTheDocument();
    expect(
      headerOf(calledWith(calls, "DELETE /api/v1/policy-assignments/1")[0], "X-CSRF-Token"),
    ).toBe(analyst.csrf_token);
  });
});

describe("governance: where baselines apply", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

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

  function effect(
    version: number,
    status: TreeEffect["status"],
    assignedTo: Target,
    replaces: Target | null = null,
  ): TreeEffect {
    return {
      name: "starter-baseline",
      assignment_id: version,
      policy_id: version,
      version,
      yaml_digest: String(version).repeat(64),
      status,
      assigned_to: assignedTo,
      replaces: replaces
        ? [
            {
              assignment_id: 1,
              policy_id: 1,
              name: "starter-baseline",
              version: 1,
              yaml_digest: "1".repeat(64),
              assigned_to: replaces,
              outcome: "overridden",
            },
          ]
        : [],
    };
  }

  function treeNode(gitlabId: number, path: string, parent: number | null, inEffect: TreeEffect[]) {
    return {
      target: {
        target_type: "group" as const,
        target_id: gitlabId,
        path,
        state: "active",
      },
      parent_gitlab_id: parent,
      ancestry: "complete" as const,
      gaps: [],
      assignments: [],
      in_effect: inEffect,
    };
  }

  const tree: AssignmentTree = {
    instance,
    instance_assignments: [
      {
        assignment_id: 1,
        policy_id: 1,
        name: "starter-baseline",
        version: 1,
        yaml_digest: "1",
      },
    ],
    groups: [
      treeNode(10, "corporate", null, [effect(1, "inherited", instance)]),
      treeNode(42, "platform", null, [effect(2, "set_here", platform, instance)]),
      treeNode(43, "platform/api", 42, [effect(2, "inherited", platform)]),
    ],
    projects: [],
  };

  const treeRoutes: Routes = {
    [TREE]: () => json(tree),
  };

  it("shows what is set at each level and what is inherited", async () => {
    api(auditor, treeRoutes);
    renderAt("/governance/where-baselines-apply");

    const tree = await screen.findByRole("list", { name: "Where baselines apply" });
    expect(within(tree).getByText("This instance")).toBeInTheDocument();
    const row = (path: string) => within(tree).getByText(path).closest("li");
    expect(row("corporate")).toHaveTextContent("starter-baseline v1inherited from This instance");
    expect(row("platform")).toHaveTextContent(
      "starter-baseline v2set herereplaces v1 from This instance",
    );
    expect(row("platform/api")).toHaveTextContent("starter-baseline v2inherited from platform");
    expect(within(tree).getByRole("list", { name: "Subgroups of platform" })).toContainElement(
      row("platform/api"),
    );
    expect(screen.queryByRole("button", { name: /Apply a baseline to/ })).not.toBeInTheDocument();
  });

  it("collapses and filters the tree", async () => {
    api(auditor, treeRoutes);
    const user = userEvent.setup();
    renderAt("/governance/where-baselines-apply");

    const toggle = await screen.findByRole("button", { name: "Subgroups of platform" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("platform/api")).not.toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Find a group" }), "api");
    expect(screen.getByText("platform/api")).toBeInTheDocument();
    expect(screen.queryByText("corporate")).not.toBeInTheDocument();
  });

  it("applies a baseline to a group from its row", async () => {
    const calls = api(analyst, {
      ...treeRoutes,
      [ASSIGN]: () =>
        json(assignment(5, { target_type: "group", target_id: 10, target_path: "corporate" }), 201),
    });
    const user = userEvent.setup();
    renderAt("/governance/where-baselines-apply");

    await user.click(await screen.findByRole("button", { name: "Apply a baseline to corporate" }));
    const form = screen.getByRole("form", { name: "Apply a baseline to corporate" });
    await user.click(await within(form).findByRole("button", { name: "Apply here" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Applied starter-baseline v2 to group corporate.",
    );
    expect(bodyOf(calledWith(calls, ASSIGN)[0])).toEqual({
      policy_name: "starter-baseline",
      version: 2,
      target_type: "group",
      target_id: 10,
    });
  });

  it("looks up what is checked for one project, and why", async () => {
    const resolution: Resolution = {
      target: { target_type: "project", target_id: 7, path: "platform/api", state: "active" },
      ancestry: "incomplete",
      gaps: [{ gitlab_id: 99, reason: "inaccessible" }],
      path: [
        { target_type: "project", target_id: 7, path: "platform/api", state: "active" },
        { target_type: "group", target_id: 42, path: "platform", state: "active" },
        { target_type: "instance", target_id: 1, path: "https://gitlab.example.test", state: null },
      ],
      policies: [
        {
          assignment_id: 2,
          policy_id: 2,
          name: "starter-baseline",
          version: 2,
          yaml_digest: "2".repeat(64),
          assigned_to: { target_type: "group", target_id: 42, path: "platform", state: "active" },
          outcome: "effective",
        },
      ],
      considered: [
        {
          assignment_id: 1,
          policy_id: 2,
          name: "starter-baseline",
          version: 1,
          yaml_digest: "1".repeat(64),
          assigned_to: {
            target_type: "instance",
            target_id: 1,
            path: "https://gitlab.example.test",
            state: null,
          },
          outcome: "overridden",
        },
      ],
    };
    api(auditor, {
      ...treeRoutes,
      [PROJECT_SEARCH]: () => json({ items: [project(7, "platform/api")], next_after_id: null }),
      "GET /api/v1/gitlab/projects/7/effective-policies": () => json(resolution),
    });
    const user = userEvent.setup();
    renderAt("/governance/where-baselines-apply");

    const panel = await screen.findByRole("region", { name: "Check a project or group" });
    await user.type(
      within(panel).getByRole("searchbox", { name: "Find a project by path" }),
      "api",
    );
    await user.click(within(panel).getByRole("button", { name: "Search projects" }));
    await user.click(await within(panel).findByRole("radio", { name: /platform\/api/ }));

    expect(await within(panel).findByLabelText("Inheritance chain")).toHaveTextContent(
      "This instance ▸ platform ▸ platform/api",
    );
    expect(within(panel).getByText(/Partly unknown/)).toHaveTextContent(
      "group 99 is not readable with Baluarte's token",
    );
    expect(within(panel).getByRole("link", { name: "starter-baseline v2" })).toBeInTheDocument();
    expect(within(panel).getByText(/— from platform/)).toBeInTheDocument();
    expect(
      within(panel).getByText(
        "starter-baseline v1 from This instance: Replaced by a closer setting",
      ),
    ).toBeInTheDocument();
  });
});
