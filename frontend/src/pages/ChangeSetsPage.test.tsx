import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChangeSet, ChangeSetSummary } from "../api/changeSets";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { selectionFrom } from "./ChangeSetsPage";

const ME = "GET /api/v1/auth/me";
const LIST = "GET /api/v1/template-change-sets";
const SAVED = "GET /api/v1/template-selections";
const PREVIEW = "POST /api/v1/template-selections/preview";
const CREATE = "POST /api/v1/template-change-sets";

const planner: CurrentUser = {
  ...analyst,
  user_id: 1,
  permissions: [...analyst.permissions, "changes:plan"],
};
const approver: CurrentUser = {
  ...analyst,
  user_id: 2,
  roles: ["admin"],
  permissions: [...analyst.permissions, "changes:plan", "changes:approve"],
};

const created = "2026-11-24T15:00:00Z";
const summary: ChangeSetSummary = {
  id: 7,
  connection_id: 1,
  title: "Tighten platform",
  state: "proposed",
  digest: "b".repeat(64),
  targets: 2,
  changes: 2,
  skipped: 1,
  sensitive: true,
  approvals_needed: 2,
  source: "service@1",
  created_by: { user_id: 1, display_name: "Ana" },
  created_at: created,
  expires_at: "2026-11-27T15:00:00Z",
  first_approved_by: null,
  first_approved_at: null,
  first_approval_note: null,
  decided_by: null,
  decided_at: null,
  decision_note: null,
  development_decision: false,
  ended_at: null,
  changing_targets: 1,
  stage_targets: 0,
  applied_by: null,
  apply_started_at: null,
  finished_at: null,
  cancelled_by: null,
  cancelled_at: null,
  rollback_by: null,
  rollback_started_at: null,
  halt_reason: null,
};
const changeSet: ChangeSet = {
  ...summary,
  items: [],
  irreversible: [],
  write_refusals: [],
  document: {
    format: 1,
    connection: { id: 1, provider: "gitlab" },
    selection: { groups: ["platform"] },
    source: { template: "service@1", template_id: 3, settings: {} },
    targets: [
      {
        project: { id: 55, gitlab_id: 101, path: "platform/site" },
        changes: [
          {
            key: "visibility",
            before: "public",
            after: "private",
            observed_at: created,
            warnings: [
              {
                code: "removes_access",
                text: "Narrows visibility from public to private: people who are not members lose access.",
              },
            ],
          },
          {
            key: "default_branch_protected",
            before: false,
            after: true,
            observed_at: created,
            warnings: [],
          },
        ],
        skipped: [
          {
            key: "security_features",
            code: "unsupported",
            reason: "A GitHub setting; GitLab security scanning is set by policies.",
          },
        ],
        unchanged: [],
      },
      {
        project: { id: 56, gitlab_id: 100, path: "platform/api" },
        changes: [],
        skipped: [],
        unchanged: ["visibility", "default_branch_protected"],
      },
    ],
  },
};

function api(user: CurrentUser, routes: Record<string, (init?: RequestInit) => Response> = {}) {
  return mockApi({
    [ME]: () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [LIST]: () => json({ change_sets: [summary] }),
    [SAVED]: () => json({ selections: [] }),
    "GET /api/v1/template-change-sets/7": () => json(changeSet),
    ...routes,
  });
}

function renderPage() {
  const router = createMemoryRouter(routes, { initialEntries: ["/governance/change-sets"] });
  render(<RouterProvider router={router} />);
}

function bodyOf(calls: Call[], key: string): unknown {
  const call = calls.find((c) => `${c.init?.method ?? "GET"} ${c.url}` === key);
  return JSON.parse(String(call?.init?.body));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("selectionFrom", () => {
  it("keeps only the criteria given", () => {
    expect(
      selectionFrom({
        groups: "platform, tools/",
        projects: "a/b\n\n c/d ",
        tags: "",
        template: " service ",
        drift: ["drift"],
        findings: "",
        policy: "",
      }),
    ).toEqual({
      groups: ["platform", "tools/"],
      projects: ["a/b", "c/d"],
      template: "service",
      drift: ["drift"],
    });
  });
});

describe("ChangeSetsPage", () => {
  it("shows a set's changes, warnings and skips, and approves the digest reviewed", async () => {
    const user = userEvent.setup();
    const calls = api(approver, {
      "POST /api/v1/template-change-sets/7/approve": () =>
        json({
          ...changeSet,
          first_approved_by: { user_id: 2, display_name: "Bea" },
          approvals_needed: 1,
        }),
    });
    renderPage();

    const list = await screen.findByRole("table", { name: "Change sets" });
    expect(within(list).getByText("sensitive")).toBeInTheDocument();
    expect(within(list).getByText("2 approvals needed")).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "Open Tighten platform" }));

    const changes = await screen.findByRole("table", { name: "Changes to platform/site" });
    expect(within(changes).getByText("public")).toBeInTheDocument();
    expect(within(changes).getByText("Removes access")).toBeInTheDocument();
    expect(screen.getByText(/skipped, not on this provider/)).toBeInTheDocument();
    expect(screen.getByText("1 projects with nothing to change")).toBeInTheDocument();
    expect(
      screen.getByText(/two people other than its creator must approve it/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve change set Tighten platform" }));
    await screen.findByText("Recorded the first approval of change set Tighten platform.");
    expect(bodyOf(calls, "POST /api/v1/template-change-sets/7/approve")).toEqual({
      digest: "b".repeat(64),
    });
  });

  it("lets the creator withdraw but not approve", async () => {
    const user = userEvent.setup();
    api(
      { ...approver, user_id: 1 },
      {
        "POST /api/v1/template-change-sets/7/withdraw": () =>
          json({ ...changeSet, state: "withdrawn" }),
      },
    );
    renderPage();

    const list = await screen.findByRole("table", { name: "Change sets" });
    await user.click(within(list).getByRole("button", { name: "Open Tighten platform" }));
    await screen.findByRole("table", { name: "Changes to platform/site" });
    expect(screen.queryByRole("button", { name: /Approve change set/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Withdraw" }));
    await screen.findByText("Withdrew change set Tighten platform.");
  });

  it("previews a selection and builds a change set from a template version", async () => {
    const user = userEvent.setup();
    const calls = api(planner, {
      [PREVIEW]: () =>
        json({
          projects: 2,
          listed: [
            { id: 55, gitlab_id: 101, path: "platform/site" },
            { id: 56, gitlab_id: 100, path: "platform/api" },
          ],
          truncated: false,
        }),
      [CREATE]: () => json(changeSet, 201),
    });
    renderPage();

    const form = await screen.findByRole("form", { name: "Build a change set" });
    const build = within(form).getByRole("button", { name: "Build change set" });
    expect(build).toBeDisabled();
    await user.type(within(form).getByLabelText("Title"), "Tighten platform");
    await user.type(within(form).getByLabelText("Groups"), "platform");
    await user.click(within(form).getByRole("checkbox", { name: "drift" }));
    await user.click(within(form).getByRole("button", { name: "Preview projects" }));
    const region = await within(form).findByRole("region", { name: "Selected projects" });
    expect(within(region).getByText("2 projects selected")).toBeInTheDocument();
    expect(bodyOf(calls, PREVIEW)).toEqual({ groups: ["platform"], drift: ["drift"] });

    await user.type(within(form).getByLabelText("Template version"), "service@1");
    await user.click(build);
    await screen.findByText("Built change set Tighten platform: 2 changes.");
    expect(bodyOf(calls, CREATE)).toEqual({
      title: "Tighten platform",
      selection: { groups: ["platform"], drift: ["drift"] },
      template: "service@1",
      expires_in_hours: 72,
    });
    expect(
      await screen.findByRole("table", { name: "Changes to platform/site" }),
    ).toBeInTheDocument();
  });

  it("hides building from those who cannot plan", async () => {
    api({ ...analyst, roles: ["auditor"], permissions: ["findings:read"] });
    renderPage();

    await screen.findByRole("table", { name: "Change sets" });
    expect(screen.queryByRole("form", { name: "Build a change set" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Change sets" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("shows why an approved set cannot be applied while writes are off", async () => {
    const user = userEvent.setup();
    const approved: ChangeSet = {
      ...changeSet,
      state: "approved",
      approvals_needed: 0,
      write_refusals: ["writes_disabled", "token_missing"],
    };
    api(approver, {
      [LIST]: () => json({ change_sets: [{ ...summary, state: "approved" }] }),
      "GET /api/v1/template-change-sets/7": () => json(approved),
    });
    renderPage();

    const list = await screen.findByRole("table", { name: "Change sets" });
    await user.click(within(list).getByRole("button", { name: "Open Tighten platform" }));
    expect(await screen.findByText("Cannot write now")).toBeInTheDocument();
    expect(screen.getByText(/BALUARTE_TEMPLATE_WRITES/)).toBeInTheDocument();
    expect(screen.getByText("No template write token is stored (Settings).")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Apply change set Tighten platform" }),
    ).toBeDisabled();
  });

  it("applies in stages, shows progress and rolls back", async () => {
    const user = userEvent.setup();
    let current: ChangeSet = { ...changeSet, state: "approved", approvals_needed: 0 };
    const item = {
      id: 1,
      position: 0,
      target_index: 0,
      project_path: "platform/site",
      setting: "visibility",
      before: "public",
      after: "private",
      irreversible: null,
      status: "applied" as const,
      attempts: 1,
      request_status: 200,
      observed: "private",
      message: "Written and read back as wanted.",
      finished_at: created,
      rollback_status: null,
      rollback_message: null,
      rolled_back_at: null,
    };
    const paused: ChangeSet = {
      ...current,
      state: "paused",
      stage_targets: 1,
      applied_by: { user_id: 2, display_name: "Bea" },
      apply_started_at: created,
      items: [item],
    };
    const calls = api(approver, {
      [LIST]: () => json({ change_sets: [{ ...summary, state: "approved" }] }),
      "GET /api/v1/template-change-sets/7": () => json(current),
      "POST /api/v1/template-change-sets/7/apply": () => {
        current = paused;
        return json(current);
      },
      "POST /api/v1/template-change-sets/7/rollback": () => {
        current = {
          ...paused,
          state: "rolling_back",
          rollback_by: { user_id: 2, display_name: "Bea" },
        };
        return json(current);
      },
    });
    renderPage();

    const list = await screen.findByRole("table", { name: "Change sets" });
    await user.click(within(list).getByRole("button", { name: "Open Tighten platform" }));
    await user.click(
      await screen.findByRole("button", { name: "Apply change set Tighten platform" }),
    );
    await screen.findByText("Started applying change set Tighten platform.");
    expect(bodyOf(calls, "POST /api/v1/template-change-sets/7/apply")).toEqual({
      stage_targets: 1,
      acknowledge_irreversible: false,
    });

    const progress = await screen.findByRole("table", {
      name: "Each change and what happened to it",
    });
    expect(within(progress).getByText("Applied")).toBeInTheDocument();
    expect(screen.getByText(/1 of 1 projects included so far/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Roll back" }));
    await screen.findByText("Started rolling back change set Tighten platform.");
  });
});
