import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Plan, PlanStep, SkippedFinding } from "../api/remediation";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { evaluation, finding, findingDetail, observation } from "../test/findings";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const PLANS = "GET /api/v1/remediation/plans?limit=50";
const PLAN = "GET /api/v1/remediation/plans/7";
const DIGEST = "d".repeat(64);

const planner: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "remediation:plan"],
  user_id: 1,
};
const approver: CurrentUser = {
  ...analyst,
  subject: "user-2",
  display_name: "Grace Hopper",
  roles: ["admin"],
  permissions: [...planner.permissions, "remediation:approve"],
  csrf_token: "a".repeat(64),
  user_id: 2,
};
const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:export", "evidence:read", "findings:read", "inventory:read"],
};

const visibilityStep: PlanStep = {
  step: 1,
  action: "restrict_project_visibility",
  target: { project_gitlab_id: 105, project_path: "platform/api" },
  finding_id: 5,
  evaluation_id: 20,
  observation_id: 10,
  observation_source: "GET /projects/:id",
  observation_digest: "e".repeat(64),
  observed_at: "2026-10-29T06:00:00+00:00",
  policy: { name: "corporate-default", version: 2, policy_id: 1 },
  rule: { id: "project-visibility", type: "gitlab.project.visibility", severity: "high" },
  current: { visibility: "public" },
  intended: { visibility: "private" },
  expected: ["private"],
  request: { method: "PUT", path: "/projects/105", body: { visibility: "private" } },
  verify: { method: "GET", path: "/projects/105", expect: { visibility: "private" } },
  preconditions: ["GET /projects/105 still reports visibility public"],
  effects: ["Non-members lose access at once."],
  requires: { role: "Owner", tier: "all" },
  idempotency_key: "restrict_project_visibility:1:105:0123456789abcdef",
};

const skipped: SkippedFinding = {
  finding_id: 6,
  project_path: "platform/docs",
  policy_name: "corporate-default",
  rule_id: "project-visibility",
  reason_code: "latest_unknown",
  reason: "The latest result is unknown, not fail; only a known failure is planned.",
};

function plan(extra: Partial<Plan> = {}): Plan {
  return {
    id: 7,
    state: "submitted",
    steps: 1,
    skipped: 1,
    plan_digest: DIGEST,
    created_by_user_id: 1,
    created_at: "2026-10-29T08:00:00Z",
    expires_at: "2026-10-30T08:00:00Z",
    submitted_at: "2026-10-29T08:05:00Z",
    decided_by_user_id: null,
    decided_at: null,
    decision_note: null,
    approved_digest: null,
    digest_valid: true,
    stale_reasons: [],
    summary: ["~ platform/api: visibility public -> private (PUT /projects/105; finding 5)"],
    plan: {
      format: "baluarte.remediation-plan/v1",
      dry_run: true,
      instance: "https://gitlab.example.test",
      created_at: "2026-10-29T08:00:00+00:00",
      expires_at: "2026-10-30T08:00:00+00:00",
      created_by: "user:1",
      scope: { finding_ids: [5, 6] },
      steps: [visibilityStep],
      skipped: [skipped],
      excluded_actions: {
        "group visibility": "left as a manual change",
      },
      execution: "Not available: executing a plan needs a separate write credential.",
    },
    ...extra,
  };
}

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, routes: Routes): Call[] {
  return mockApi({
    [ME]: () => json(user),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function posts(calls: Call[]): Call[] {
  return calls.filter((call) => call.init?.method === "POST");
}

function bodyOf(call: Call | undefined): unknown {
  return JSON.parse(String(call?.init?.body));
}

function headerOf(call: Call | undefined, name: string): string | undefined {
  return (call?.init?.headers as Record<string, string> | undefined)?.[name];
}

const dryRun = /Baluarte makes no change to GitLab/;

describe("remediation plans list", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists plans under the dry-run notice and offers creation to planners", async () => {
    api(planner, {
      [PLANS]: () =>
        json({
          items: [plan(), plan({ id: 8, state: "expired", created_by_user_id: 2 })],
          next_after_id: null,
        }),
    });
    renderAt("/remediation");

    const table = await screen.findByRole("table", { name: "Remediation plans" });
    expect(screen.getByRole("region", { name: "Dry run only" })).toHaveTextContent(dryRun);
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("Submitted");
    expect(rows[1]).toHaveTextContent("User 1 (you)");
    expect(rows[2]).toHaveTextContent("Expired");
    expect(rows[2]).toHaveTextContent("30 Oct 2026, 08:00 UTC");
    expect(screen.getByRole("link", { name: "Create a dry-run plan" })).toHaveAttribute(
      "href",
      "/remediation/new",
    );
    expect(screen.getByRole("link", { name: "Remediation" })).toBeInTheDocument();
  });

  it("does not offer creation without the planning permission", async () => {
    api(auditor, { [PLANS]: () => json({ items: [], next_after_id: null }) });
    renderAt("/remediation");

    expect(await screen.findByText("No remediation plans have been created.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a dry-run plan" })).not.toBeInTheDocument();
    expect(screen.getByText(/Creating plans needs the remediation planning/)).toBeInTheDocument();
  });

  it("filters by state through the URL", async () => {
    const user = userEvent.setup();
    const calls = api(planner, {
      [PLANS]: () => json({ items: [plan()], next_after_id: null }),
      "GET /api/v1/remediation/plans?state=approved&limit=50": () =>
        json({ items: [], next_after_id: null }),
    });
    const router = renderAt("/remediation");

    await screen.findByRole("table", { name: "Remediation plans" });
    await user.selectOptions(screen.getByRole("combobox", { name: "State" }), "approved");
    await user.click(screen.getByRole("button", { name: "Apply filter" }));

    expect(await screen.findByText("No plans are in this state.")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?state=approved");
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/remediation/plans?state=approved&limit=50",
    );
  });
});

describe("a remediation plan", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows every step as a diff with the request that would be sent", async () => {
    api(auditor, { [PLAN]: () => json(plan()) });
    renderAt("/remediation/7");

    const step = await screen.findByRole("article", {
      name: "Step 1: Restrict project visibility for platform/api",
    });
    expect(screen.getByRole("region", { name: "Dry run only" })).toHaveTextContent(dryRun);
    expect(within(step).getByText("visibility: public")).toBeInTheDocument();
    expect(within(step).getByText("visibility: private")).toBeInTheDocument();
    const request = within(step).getByLabelText("Request for step 1");
    expect(request.textContent).toBe('PUT /projects/105\n{\n  "visibility": "private"\n}');
    expect(within(step).getByText("Not sent: execution is not available.")).toBeInTheDocument();
    expect(
      within(step).getByText("GET /projects/105 still reports visibility public"),
    ).toBeInTheDocument();
    expect(within(step).getByText("Non-members lose access at once.")).toBeInTheDocument();
    expect(within(step).getByRole("link", { name: "Finding 5" })).toHaveAttribute(
      "href",
      "/findings/5",
    );
    expect(screen.getByText(DIGEST)).toBeInTheDocument();
    expect(screen.getByLabelText("Plan summary")).toHaveTextContent(
      "~ platform/api: visibility public -> private",
    );

    const skippedTable = screen.getByRole("table", { name: "Skipped findings" });
    expect(skippedTable).toHaveTextContent("Latest result unknown");
    expect(skippedTable).toHaveTextContent("only a known failure is planned");
    expect(screen.getByRole("region", { name: "Never planned" })).toHaveTextContent(
      "group visibility",
    );
    // An auditor may read plans but not decide them.
    expect(screen.queryByRole("button", { name: "Approve…" })).not.toBeInTheDocument();
  });

  it("binds an approval to the digest and sends it with the CSRF token", async () => {
    const user = userEvent.setup();
    const calls = api(approver, {
      [PLAN]: () => json(plan()),
      "POST /api/v1/remediation/plans/7/approve": () =>
        json(plan({ state: "approved", approved_digest: DIGEST, decided_by_user_id: 2 })),
    });
    renderAt("/remediation/7");

    await user.click(await screen.findByRole("button", { name: "Approve…" }));
    expect(screen.getByRole("heading", { name: "Approve this plan?" })).toHaveFocus();
    const confirm = screen.getByRole("form", { name: "Approve this plan?" });
    expect(confirm).toHaveTextContent(`Your approval binds to plan digest ${DIGEST}`);
    expect(confirm).toHaveTextContent("Approving changes nothing in GitLab");
    await user.type(within(confirm).getByRole("textbox", { name: "Note (optional)" }), " ok ");
    await user.click(within(confirm).getByRole("button", { name: "Confirm approval" }));

    const status = await screen.findByText("Plan 7 approved. Nothing was changed in GitLab.");
    expect(status).toHaveFocus();
    const [call] = posts(calls);
    expect(bodyOf(call)).toEqual({ plan_digest: DIGEST, note: "ok" });
    expect(headerOf(call, "X-CSRF-Token")).toBe(approver.csrf_token);
    expect(screen.getByText(/^Approved: Approved for the digest shown/)).toBeInTheDocument();
  });

  it("explains a stale plan when approval is refused", async () => {
    const user = userEvent.setup();
    api(approver, {
      [PLAN]: () => json(plan()),
      "POST /api/v1/remediation/plans/7/approve": () =>
        json(
          {
            detail: {
              message: "The plan is stale and cannot be approved",
              reasons: ["finding 5 is no longer open and failing", "newer evidence for step 1"],
            },
          },
          409,
        ),
    });
    renderAt("/remediation/7");

    await user.click(await screen.findByRole("button", { name: "Approve…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Not possible now: The plan is stale and cannot be approved.");
    expect(
      within(alert)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["finding 5 is no longer open and failing", "newer evidence for step 1"]);
    expect(alert).toHaveFocus();
  });

  it("warns before approval when the API already reports the plan stale", async () => {
    api(approver, {
      [PLAN]: () => json(plan({ stale_reasons: ["finding 5 is no longer open and failing"] })),
    });
    renderAt("/remediation/7");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This plan is stale and would be refused for approval");
    expect(alert).toHaveTextContent("finding 5 is no longer open and failing");
  });

  it("hides the decision from the plan's creator", async () => {
    api({ ...approver, user_id: 1 }, { [PLAN]: () => json(plan()) });
    renderAt("/remediation/7");

    expect(
      await screen.findByText("You created this plan. Someone else must approve or reject it."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve…" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject…" })).not.toBeInTheDocument();
  });

  it("requires a reason to reject and shows the API's refusal", async () => {
    const user = userEvent.setup();
    api(approver, {
      [PLAN]: () => json(plan()),
      "POST /api/v1/remediation/plans/7/reject": () =>
        json({ detail: "The plan's creator cannot decide it" }, 403),
    });
    renderAt("/remediation/7");

    await user.click(await screen.findByRole("button", { name: "Reject…" }));
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Give a reason; it is recorded with the rejection.",
    );
    await user.type(screen.getByRole("textbox", { name: "Reason" }), "Not now");
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Refused: The plan's creator cannot decide it.",
    );
  });

  it("lets the creator submit a draft", async () => {
    const user = userEvent.setup();
    const calls = api(planner, {
      [PLAN]: () => json(plan({ state: "draft", submitted_at: null })),
      "POST /api/v1/remediation/plans/7/submit": () => json(plan()),
    });
    renderAt("/remediation/7");

    await user.click(await screen.findByRole("button", { name: "Submit for approval…" }));
    await user.click(screen.getByRole("button", { name: "Confirm submission" }));

    expect(
      await screen.findByText("Plan 7 submitted for approval. Nothing was changed in GitLab."),
    ).toBeInTheDocument();
    const [call] = posts(calls);
    expect(call?.url).toBe("/api/v1/remediation/plans/7/submit");
    expect(bodyOf(call)).toEqual({});
    expect(headerOf(call, "X-CSRF-Token")).toBe(planner.csrf_token);
  });

  it("offers submission only to the creator", async () => {
    api({ ...planner, user_id: 3 }, { [PLAN]: () => json(plan({ state: "draft" })) });
    renderAt("/remediation/7");

    await screen.findByRole("region", { name: "Summary" });
    expect(screen.queryByRole("button", { name: "Submit for approval…" })).not.toBeInTheDocument();
  });

  it("offers no decision on an expired plan and shows when it expired", async () => {
    api(approver, { [PLAN]: () => json(plan({ state: "expired" })) });
    renderAt("/remediation/7");

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(summary).toHaveTextContent("Expired: Past its expiry");
    expect(summary).toHaveTextContent("30 Oct 2026, 08:00 UTC");
    expect(screen.queryByRole("region", { name: "Decision" })).not.toBeInTheDocument();
  });

  it("refuses to offer approval when the stored plan fails its digest", async () => {
    api(approver, { [PLAN]: () => json(plan({ digest_valid: false })) });
    renderAt("/remediation/7");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The stored plan no longer matches its digest",
    );
    expect(screen.queryByRole("button", { name: "Approve…" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject…" })).toBeInTheDocument();
  });

  it("says when a plan does not exist", async () => {
    api(auditor, { [PLAN]: () => json({ detail: "No such remediation plan" }, 404) });
    renderAt("/remediation/7");

    expect(await screen.findByText("There is no remediation plan with ID 7.")).toBeInTheDocument();
  });
});

describe("creating a remediation plan", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("prefills the scope from the URL, validates it and sends exactly what was given", async () => {
    const user = userEvent.setup();
    const calls = api(planner, {
      "POST /api/v1/remediation/plans": () => json(plan({ state: "draft" }), 201),
      [PLAN]: () => json(plan({ state: "draft" })),
    });
    const router = renderAt("/remediation/new?project=105&policy=corporate-default&rule=r1");

    const project = await screen.findByRole("textbox", { name: "Project GitLab ID" });
    expect(project).toHaveValue("105");
    expect(screen.getByRole("region", { name: "Dry run only" })).toHaveTextContent(dryRun);

    await user.type(screen.getByRole("textbox", { name: /Finding IDs/ }), "5, 6");
    await user.click(screen.getByRole("button", { name: "Build dry-run plan" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Give either finding IDs or filters, not both.",
    );
    expect(posts(calls)).toHaveLength(0);

    await user.clear(screen.getByRole("textbox", { name: /Finding IDs/ }));
    await user.click(screen.getByRole("button", { name: "Build dry-run plan" }));

    expect(
      await screen.findByText(
        "Plan 7 created as a draft. Review it, then submit it for approval. Nothing was changed in GitLab.",
      ),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/remediation/7");
    const [call] = posts(calls);
    expect(bodyOf(call)).toEqual({
      scope: { project_gitlab_id: 105, policy: "corporate-default", rule: "r1" },
      expires_in_hours: 24,
    });
    expect(headerOf(call, "X-CSRF-Token")).toBe(planner.csrf_token);
  });

  it("lists why each finding was skipped when nothing can be planned", async () => {
    const user = userEvent.setup();
    api(planner, {
      "POST /api/v1/remediation/plans": () =>
        json(
          { detail: { message: "No finding in scope can be remediated", skipped: [skipped] } },
          422,
        ),
    });
    renderAt("/remediation/new?finding=6");

    expect(await screen.findByRole("textbox", { name: /Finding IDs/ })).toHaveValue("6");
    await user.click(screen.getByRole("button", { name: "Build dry-run plan" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Refused: No finding in scope can be remediated");
    const table = within(alert).getByRole("table", { name: "Findings that cannot be planned" });
    expect(table).toHaveTextContent("Latest result unknown");
    expect(alert).toHaveFocus();
  });

  it("explains that planning needs a permission", async () => {
    api(auditor, {});
    renderAt("/remediation/new");

    expect(
      await screen.findByText(/Creating plans needs the remediation planning permission/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Build dry-run plan" })).not.toBeInTheDocument();
  });
});

describe("planning from findings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("links a filtered findings view to the plan form and says what it ignores", async () => {
    api(planner, {
      "GET /api/v1/findings?state=open&project=105&rule=r1&limit=50": () =>
        json({ items: [finding(5)], next_after_id: null }),
    });
    renderAt("/findings?state=open&project=105&rule=r1");

    const link = await screen.findByRole("link", { name: "Plan a dry-run fix for this view" });
    expect(link).toHaveAttribute("href", "/remediation/new?project=105&rule=r1");
    expect(link.parentElement).toHaveTextContent("A plan cannot filter by state");
  });

  it("offers no plan link to users without the permission or for an unfiltered view", async () => {
    api(planner, {
      "GET /api/v1/findings?limit=50": () => json({ items: [finding(5)], next_after_id: null }),
    });
    renderAt("/findings");

    await screen.findByRole("table");
    expect(screen.queryByRole("link", { name: /Plan a dry-run fix/ })).not.toBeInTheDocument();
  });

  it("links an open, failing finding to a plan for that finding", async () => {
    const detail = findingDetail(5, evaluation(10, { outcome: "fail" }));
    api(planner, {
      "GET /api/v1/findings/5": () => json(detail),
      "GET /api/v1/findings/5/evaluations?limit=50": () =>
        json({ items: [detail.latest_evaluation], next_before_id: null }),
      [`GET /api/v1/gitlab/projects/${String(detail.project.gitlab_id)}/observations?before_id=${String((detail.latest_evaluation.observation_id ?? 0) + 1)}&limit=1`]:
        () =>
          json({
            items: [observation(detail.latest_evaluation.observation_id ?? 9)],
            next_before_id: null,
          }),
      [`GET /api/v1/exceptions?project=${String(detail.project.gitlab_id)}&policy=corporate-default&rule=project-visibility&limit=50`]:
        () => json({ items: [], next_after_id: null }),
    });
    renderAt("/findings/5");

    expect(
      await screen.findByRole("link", { name: "Plan a dry-run fix for this finding" }),
    ).toHaveAttribute("href", "/remediation/new?finding=5");
  });
});
