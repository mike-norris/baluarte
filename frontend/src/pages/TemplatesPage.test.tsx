import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  Alignment,
  EffectiveSettings,
  ProjectDrift,
  TemplateException,
  SettingSupport,
  TemplateAssignment,
  TemplateVersion,
} from "../api/templates";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { overridesFrom, valueText } from "./TemplatesPage";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const TEMPLATES = "GET /api/v1/templates";
const SERVICE_1 = "GET /api/v1/templates/service/versions/1";
const SUPPORT = "GET /api/v1/templates/settings";
const ASSIGNMENTS = "GET /api/v1/template-assignments";
const SAVE = "POST /api/v1/templates";
const ASSIGN = "POST /api/v1/template-assignments";
const ALIGNMENT = "GET /api/v1/templates/alignment";
const EXCEPTIONS = "GET /api/v1/template-exceptions";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:read", "findings:read", "inventory:read"],
};

const created = "2026-11-24T15:00:00Z";
const summary = {
  name: "service",
  version: 1,
  yaml_digest: "a".repeat(64),
  extends: "base@1",
  created_by: "user:1",
  created_at: created,
};
const service: TemplateVersion = {
  ...summary,
  description: "Services that ship to production",
  source_yaml: "apiVersion: baluarte.io/v1alpha1\n",
  settings: { "merge_requests.min_approvals": 2 },
  resolved_settings: { "merge_requests.min_approvals": 2, visibility: "private" },
  chain: ["base@1"],
};
const support: SettingSupport[] = [
  {
    key: "merge_requests.forbid_override",
    gitlab: "plan",
    gitlab_note: "Approval rules need GitLab Premium or Ultimate.",
    github: "unsupported",
    github_note: "GitHub has no equivalent setting.",
  },
];
const assignment: TemplateAssignment = {
  id: 7,
  connection_id: 1,
  name: "service",
  version: 1,
  target: { target_type: "group", target_id: 11, path: "platform/backend", state: "active" },
  overrides: null,
  override_reason: null,
  assigned_by: "user:1",
  created_at: created,
};

const tally = { aligned: 0, drift: 0, excepted: 0, unknown: 0, unsupported: 0 };
const svc = { id: 55, gitlab_id: 100, path: "platform/backend/svc" };
const alignment: Alignment = {
  connection_id: 1,
  provider: "gitlab",
  evaluated_at: created,
  projects: 3,
  projects_with_templates: 2,
  projects_aligned: 1,
  projects_drifting: 1,
  projects_unknown: 0,
  settings: { ...tally, aligned: 5, drift: 1 },
  by_template: [
    {
      name: "service v1",
      projects: 2,
      projects_drifting: 1,
      settings: { ...tally, aligned: 5, drift: 1 },
    },
  ],
  by_group: [
    {
      name: "platform",
      projects: 2,
      projects_drifting: 1,
      settings: { ...tally, aligned: 5, drift: 1 },
    },
  ],
  attention: [{ project: svc, status: "drift", counts: { drift: 1, aligned: 1 } }],
  attention_truncated: false,
  expiring_exceptions: 1,
};
const svcDrift: ProjectDrift = {
  project: svc,
  connection_id: 1,
  provider: "gitlab",
  ancestry: "complete",
  evaluated_at: created,
  status: "drift",
  counts: { drift: 1, aligned: 1 },
  settings: [
    {
      key: "visibility",
      expected: "private",
      status: "drift",
      reason: "Project platform/backend/svc is public; the policy allows private.",
      rule_type: "gitlab.project.visibility",
      observed_at: created,
      decided_by: {
        value: "private",
        origin: "template",
        template: "service",
        version: 1,
        assignment_id: 7,
        assigned_to: assignment.target,
      },
      exception: null,
    },
    {
      key: "default_branch_protected",
      expected: true,
      status: "aligned",
      reason: "Default branch main is protected by main.",
      rule_type: "gitlab.branch.default_protected",
      observed_at: created,
      decided_by: {
        value: true,
        origin: "template",
        template: "service",
        version: 1,
        assignment_id: 7,
        assigned_to: assignment.target,
      },
      exception: null,
    },
  ],
};
const svcEffective: EffectiveSettings = {
  target: { target_type: "project", target_id: 100, path: svc.path, state: "active" },
  provider: "gitlab",
  ancestry: "complete",
  gaps: [],
  path: [],
  assignments: [],
  settings: [],
};
const requested: TemplateException = {
  id: 9,
  connection_id: 1,
  project_id: 55,
  project_path: svc.path,
  setting: "visibility",
  justification: "A public documentation site.",
  state: "requested",
  expiring: false,
  requested_by: { user_id: 2, display_name: "Bea" },
  requested_at: created,
  expires_at: "2026-12-24T15:00:00Z",
  decided_by: null,
  decided_at: null,
  decision_note: null,
  development_decision: false,
  ended_by: null,
  ended_at: null,
  end_note: null,
};

function api(user: CurrentUser, routes: Record<string, (init?: RequestInit) => Response> = {}) {
  return mockApi({
    [ME]: () => json(user),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [TEMPLATES]: () =>
      json({
        templates: [{ name: "service", description: service.description, versions: [summary] }],
      }),
    [SERVICE_1]: () => json(service),
    [SUPPORT]: () => json(support),
    [ASSIGNMENTS]: () => json({ assignments: [assignment] }),
    [ALIGNMENT]: () => json(alignment),
    [EXCEPTIONS]: () => json({ exceptions: [] }),
    ...routes,
  });
}

function renderPage() {
  const router = createMemoryRouter(routes, { initialEntries: ["/governance/templates"] });
  render(<RouterProvider router={router} />);
}

function bodyOf(calls: Call[], key: string): unknown {
  const call = calls.find((c) => `${c.init?.method ?? "GET"} ${c.url}` === key);
  return JSON.parse(String(call?.init?.body));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("TemplatesPage", () => {
  it("shows templates, what they inherit, where they apply and provider support", async () => {
    api(auditor);
    renderPage();

    const card = await screen.findByRole("article", { name: "service" });
    expect(await within(card).findByText("Extends base@1.")).toBeInTheDocument();
    expect(within(card).getByText("Approvals required")).toBeInTheDocument();
    expect(within(card).getByText("(inherited)", { exact: false })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Template assignments" });
    expect(within(table).getByText("Group platform/backend")).toBeInTheDocument();
    const matrix = await screen.findByRole("table", { name: "Settings a template can hold" });
    expect(within(matrix).getByText("No equivalent")).toBeInTheDocument();
    expect(within(matrix).getByText("Depends on the plan")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Templates" })).toHaveAttribute("aria-current", "page");
  });

  it("offers no saving, assigning or removing to readers", async () => {
    api(auditor);
    renderPage();

    await screen.findByRole("table", { name: "Template assignments" });
    expect(screen.queryByRole("button", { name: "Save version" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Assign" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
  });

  it("lists the problems in an invalid template", async () => {
    const user = userEvent.setup();
    api(analyst, {
      [SAVE]: () =>
        json(
          {
            detail: {
              message: "spec.settings.merge_method: unknown field",
              errors: [{ path: "spec.settings.merge_method", message: "unknown field" }],
            },
          },
          422,
        ),
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Save version" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("spec.settings.merge_method: unknown field");
  });

  it("saves a version and reports it", async () => {
    const user = userEvent.setup();
    const calls = api(analyst, { [SAVE]: () => json(service, 201) });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Save version" }));

    expect(await screen.findByRole("status", { name: "" })).toHaveTextContent("Saved service v1.");
    expect(bodyOf(calls, SAVE)).toMatchObject({
      source_yaml: expect.stringContaining("kind: ConfigurationTemplate") as unknown,
    });
  });

  it("needs a reason before overriding a setting for a target", async () => {
    const user = userEvent.setup();
    const calls = api(analyst, {
      [ASSIGN]: () =>
        json(
          {
            ...assignment,
            target: {
              ...assignment.target,
              target_type: "instance",
              path: "https://gitlab.example.test",
            },
          },
          201,
        ),
    });
    renderPage();

    const form = await screen.findByRole("form", { name: "Assign a template" });
    await user.selectOptions(within(form).getByLabelText("Visibility"), "public");
    const assign = within(form).getByRole("button", { name: "Assign" });
    expect(assign).toBeDisabled();
    await user.type(
      within(form).getByLabelText(/Why this target differs/),
      "The SDK repository must stay public.",
    );
    await user.click(assign);

    await screen.findByText("Assigned service v1 to whole connection.");
    expect(bodyOf(calls, ASSIGN)).toEqual({
      name: "service",
      version: 1,
      target_type: "instance",
      overrides: { visibility: "public" },
      override_reason: "The SDK repository must stay public.",
    });
  });

  it("shows a project's effective settings and where each came from", async () => {
    const user = userEvent.setup();
    const effective: EffectiveSettings = {
      target: {
        target_type: "project",
        target_id: 100,
        path: "platform/backend/svc",
        state: "active",
      },
      provider: "gitlab",
      ancestry: "complete",
      gaps: [],
      path: [],
      assignments: [],
      settings: [
        {
          key: "merge_requests.min_approvals",
          value: 2,
          rule_type: "gitlab.project.merge_request_approvals",
          support: "plan",
          support_note: "Approval rules need GitLab Premium or Ultimate.",
          decided_by: {
            value: 2,
            origin: "template",
            template: "service",
            version: 1,
            assignment_id: 7,
            assigned_to: assignment.target,
          },
          replaced: [
            {
              value: 1,
              origin: "template",
              template: "base",
              version: 1,
              assignment_id: 3,
              assigned_to: {
                target_type: "instance",
                target_id: 1,
                path: "https://gitlab.example.test",
                state: null,
              },
            },
          ],
        },
      ],
    };
    const calls = api(auditor, {
      "GET /api/v1/templates/effective/55": () => json(effective),
    });
    vi.stubGlobal(
      "fetch",
      ((original) => (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input instanceof Request ? input.url : input.toString();
        if (url.startsWith("/api/v1/gitlab/projects?")) {
          calls.push({ url, init });
          return Promise.resolve(
            json({
              items: [{ id: 55, gitlab_id: 100, path_with_namespace: "platform/backend/svc" }],
              next_after_id: null,
            }),
          );
        }
        return original(input, init);
      })(fetch),
    );
    renderPage();

    await user.type(await screen.findByLabelText("Find a project by path"), "svc");
    await user.click(screen.getByRole("button", { name: "Search projects" }));
    await user.click(await screen.findByRole("button", { name: "platform/backend/svc" }));

    const table = await screen.findByRole("table", {
      name: "Effective settings of platform/backend/svc",
    });
    expect(
      within(table).getByText("service v1 (template) on group platform/backend"),
    ).toBeInTheDocument();
    expect(
      within(table).getByText(/Replaces 1 from base v1 \(template\) on whole connection/),
    ).toBeInTheDocument();
    expect(within(table).getByText("Depends on the plan")).toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("reads values and builds overrides only from what is set", () => {
    expect(valueText(true)).toBe("Yes");
    expect(valueText(["secret_scanning", "code_scanning"])).toBe("secret_scanning, code_scanning");
    expect(
      overridesFrom({ visibility: "", defaultBranchProtected: "", minApprovals: "" }),
    ).toBeUndefined();
    expect(
      overridesFrom({ visibility: "", defaultBranchProtected: "no", minApprovals: "0" }),
    ).toEqual({
      default_branch_protected: false,
      merge_requests: { min_approvals: 0 },
    });
  });
});

describe("TemplatesPage drift", () => {
  it("summarizes alignment and opens a drifting project's drift", async () => {
    const user = userEvent.setup();
    api(auditor, {
      "GET /api/v1/templates/effective/55": () => json(svcEffective),
      "GET /api/v1/templates/drift/55": () => json(svcDrift),
    });
    renderPage();

    const attention = await screen.findByRole("table", { name: "Projects needing attention" });
    expect(screen.getByText("1 exceptions end within 14 days")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Alignment by template" })).toHaveTextContent(
      "service v1",
    );
    await user.click(within(attention).getByRole("button", { name: "Show platform/backend/svc" }));

    const table = await screen.findByRole("table", { name: "Drift of platform/backend/svc" });
    expect(within(table).getByText("Drift")).toBeInTheDocument();
    expect(within(table).getByText("Aligned")).toBeInTheDocument();
    expect(within(table).getByText(/is public; the policy allows private/)).toBeInTheDocument();
    // Auditors cannot propose fixes or request exceptions.
    expect(screen.queryByRole("button", { name: /Propose a fix/ })).not.toBeInTheDocument();
    expect(
      within(table).queryByRole("button", { name: /Request an exception/ }),
    ).not.toBeInTheDocument();
  });

  it("proposes a change set that fixes a project's drift", async () => {
    const user = userEvent.setup();
    const planner: CurrentUser = {
      ...analyst,
      permissions: [...analyst.permissions, "changes:plan"],
    };
    const calls = api(planner, {
      "GET /api/v1/templates/effective/55": () => json(svcEffective),
      "GET /api/v1/templates/drift/55": () => json(svcDrift),
      "POST /api/v1/template-change-sets": () => json({ id: 7 }, 201),
      "GET /api/v1/template-change-sets": () => json({ change_sets: [] }),
      "GET /api/v1/template-selections": () => json({ selections: [] }),
      "GET /api/v1/template-change-sets/7": () => json({ detail: "gone" }, 404),
    });
    renderPage();

    expect(
      await screen.findByRole("button", { name: "Propose a fix for drifting projects" }),
    ).toBeInTheDocument();
    const attention = await screen.findByRole("table", { name: "Projects needing attention" });
    await user.click(within(attention).getByRole("button", { name: "Show platform/backend/svc" }));
    await user.click(
      await screen.findByRole("button", { name: "Propose a fix for platform/backend/svc" }),
    );

    await screen.findByText("No change sets yet");
    expect(bodyOf(calls, "POST /api/v1/template-change-sets")).toEqual({
      title: "Fix template drift in platform/backend/svc",
      selection: { projects: ["platform/backend/svc"] },
      effective: true,
      expires_in_hours: 72,
    });
  });

  it("requests an exception for a drifting setting", async () => {
    const user = userEvent.setup();
    const requester: CurrentUser = {
      ...analyst,
      permissions: [...analyst.permissions, "exception:request"],
    };
    const calls = api(requester, {
      "GET /api/v1/templates/effective/55": () => json(svcEffective),
      "GET /api/v1/templates/drift/55": () => json(svcDrift),
      "POST /api/v1/template-exceptions": () => json(requested, 201),
    });
    renderPage();

    const attention = await screen.findByRole("table", { name: "Projects needing attention" });
    await user.click(within(attention).getByRole("button", { name: "Show platform/backend/svc" }));
    const table = await screen.findByRole("table", { name: "Drift of platform/backend/svc" });
    await user.click(
      within(table).getByRole("button", { name: "Request an exception for Visibility" }),
    );
    const form = within(table).getByRole("form", { name: "Request an exception for Visibility" });
    const send = within(form).getByRole("button", { name: "Request exception" });
    expect(send).toBeDisabled();
    await user.type(
      within(form).getByLabelText("Why this project may differ"),
      "A public docs site.",
    );
    await user.click(send);

    await screen.findByText("Requested an exception for Visibility. Someone else must approve it.");
    const body = bodyOf(calls, "POST /api/v1/template-exceptions") as Record<string, unknown>;
    expect(body).toMatchObject({
      project_id: 55,
      setting: "visibility",
      justification: "A public docs site.",
    });
    expect(typeof body.expires_at).toBe("string");
  });

  it("lets an approver approve, and needs a note to reject", async () => {
    const user = userEvent.setup();
    const admin: CurrentUser = {
      ...analyst,
      roles: ["admin"],
      permissions: [...analyst.permissions, "exception:approve"],
    };
    const calls = api(admin, {
      [EXCEPTIONS]: () => json({ exceptions: [requested] }),
      "POST /api/v1/template-exceptions/9/approve": () => json({ ...requested, state: "approved" }),
    });
    renderPage();

    const table = await screen.findByRole("table", { name: "Template exceptions" });
    expect(within(table).getByRole("button", { name: "Reject (with a note)" })).toBeDisabled();
    await user.click(
      within(table).getByRole("button", {
        name: "Approve exception for Visibility on platform/backend/svc",
      }),
    );

    await screen.findByText("Approved the exception for Visibility on platform/backend/svc.");
    expect(calls.some((c) => c.url === "/api/v1/template-exceptions/9/approve")).toBe(true);
  });
});
