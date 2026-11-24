import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EffectiveSettings,
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
