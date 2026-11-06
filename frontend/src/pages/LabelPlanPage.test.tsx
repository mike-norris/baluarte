import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LabelPlan } from "../api/labels";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { DRIFT, PLANS, TAXONOMY, driftPage, labelPlan, step, taxonomyView } from "../test/labels";
import { APPLY_POLL_MS } from "./LabelPlanPage";

const planner: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "remediation:plan", "remediation:approve"],
};
const approver: CurrentUser = { ...planner, user_id: 2, display_name: "Grace Hopper" };

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, routes: Routes): Call[] {
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

function bodyOf(calls: Call[], url: string): unknown {
  const call = calls.filter((c) => c.url === url && c.init?.method === "POST").at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

describe("label plans", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("creates a plan for the chosen groups and opens it", async () => {
    const calls = api(planner, {
      [PLANS]: () => json({ items: [], next_after_id: null }),
      "POST /api/v1/labels/plans": () => json(labelPlan(4), 201),
      "GET /api/v1/labels/plans/4": () => json(labelPlan(4)),
    });
    const user = userEvent.setup();
    const router = renderAt("/labels/plans");

    expect(await screen.findByText("No label plans have been created.")).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Top-level groups/), "acme, acme-labs");
    await user.click(screen.getByRole("button", { name: "Create a plan" }));

    expect(await screen.findByRole("heading", { name: "Label plan 4" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/labels/plans/4");
    expect(bodyOf(calls, "/api/v1/labels/plans")).toEqual({
      scope: { groups: ["acme", "acme-labs"] },
    });
    expect(await screen.findByText(/created as a draft from labels read just now/)).toBeInTheDocument();
  });

  it("explains a refused plan", async () => {
    api(planner, {
      [PLANS]: () => json({ items: [], next_after_id: null }),
      "POST /api/v1/labels/plans": () =>
        json({ detail: { code: "nothing_to_plan", message: "Nothing to plan", reasons: [] } }, 409),
    });
    const user = userEvent.setup();
    renderAt("/labels/plans");

    await user.click(await screen.findByRole("button", { name: "Create a plan" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Not possible now: there is no drift to fix or report in that scope.",
    );
  });

  it("lists plans with their scope, steps and work by hand", async () => {
    api(planner, {
      [PLANS]: () =>
        json({
          items: [
            {
              id: 4,
              state: "approved",
              scope: { groups: [] },
              taxonomy: { id: 1, version: 1, name: "acme-labels" },
              steps: 2,
              manual: 1,
              plan_digest: "f".repeat(64),
              created_by: { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" },
              created_at: "2026-11-05T09:00:00Z",
              expires_at: "2026-11-06T09:00:00Z",
            },
          ],
          next_after_id: null,
        }),
    });
    renderAt("/labels/plans");

    const table = await screen.findByRole("table", { name: "Label plans" });
    const row = within(table).getAllByRole("row")[1] as HTMLElement;
    expect(row).toHaveTextContent("Plan 4ApprovedWhole taxonomy21Ada Lovelace");
    expect(within(row).getByRole("link", { name: "Plan 4" })).toHaveAttribute(
      "href",
      "/labels/plans/4",
    );
  });

  it("shows steps, work by hand and why a draft can't be applied", async () => {
    api(planner, { "GET /api/v1/labels/plans/4": () => json(labelPlan(4)) });
    renderAt("/labels/plans/4");

    const steps = await screen.findByRole("table", { name: /Plan steps/ });
    const rows = within(steps).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("Update label priority::high");
    expect(rows[1]).toHaveTextContent("colour: #ff0000");
    expect(rows[1]).toHaveTextContent("colour: #d9534f");
    expect(rows[1]).toHaveTextContent("Not run yet");
    expect(rows[2]).toHaveTextContent("Rename label type::bug");
    expect(screen.getByText(/Duplicate \(priority::high\)\. Also defined on the project/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit for approval" })).toBeEnabled();
  });

  it("lets someone other than the creator approve the digest they saw", async () => {
    const submitted = labelPlan(4, { state: "submitted", submitted_at: "2026-11-05T09:10:00Z" });
    const calls = api(approver, {
      "GET /api/v1/labels/plans/4": () => json(submitted),
      "POST /api/v1/labels/plans/4/approve": () =>
        json({ ...submitted, state: "approved", can_apply: { allowed: true, reasons: [] } }),
    });
    const user = userEvent.setup();
    renderAt("/labels/plans/4");

    await user.type(await screen.findByLabelText("Note (required to reject)"), "Checked the colours.");
    await user.click(screen.getByRole("button", { name: "Approve this digest" }));
    expect(await screen.findByText("Plan 4 approved.")).toBeInTheDocument();
    expect(bodyOf(calls, "/api/v1/labels/plans/4/approve")).toEqual({
      plan_digest: "f".repeat(64),
      note: "Checked the colours.",
    });
    expect(screen.getByRole("button", { name: "Apply in GitLab" })).toBeEnabled();
  });

  it("does not let the creator decide their own plan", async () => {
    api(planner, {
      "GET /api/v1/labels/plans/4": () => json(labelPlan(4, { state: "submitted" })),
    });
    renderAt("/labels/plans/4");

    expect(
      await screen.findByText("You created this plan. Someone else must approve or reject it."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve this digest" })).not.toBeInTheDocument();
  });

  it("says why an approved plan can't be applied", async () => {
    api(planner, {
      "GET /api/v1/labels/plans/4": () =>
        json(
          labelPlan(4, {
            state: "approved",
            can_apply: { allowed: false, reasons: ["label_writes_disabled", "write_token_missing"] },
          }),
        ),
    });
    renderAt("/labels/plans/4");

    const apply = await screen.findByRole("button", { name: "Apply in GitLab" });
    expect(apply).toBeDisabled();
    const why = document.getElementById(apply.getAttribute("aria-describedby") ?? "");
    expect(why).toHaveTextContent("BALUARTE_LABEL_WRITES");
    expect(why).toHaveTextContent("No label write token is set");
  });

  it("applies an approved plan and follows its progress", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let reads = 0;
    const approved = labelPlan(4, { state: "approved", can_apply: { allowed: true, reasons: [] } });
    const applying = { ...approved, state: "applying" as const, apply_started_at: "2026-11-05T10:00:00Z" };
    const applied: LabelPlan = {
      ...applying,
      state: "applied",
      finished_at: "2026-11-05T10:00:05Z",
      can_apply: { allowed: false, reasons: [] },
      steps: [
        step(1, { status: "applied", request_status: 200, verified: true }),
        step(2, { action: "rename", label: "type::bug", status: "skipped", verified: true }),
      ],
    };
    const calls = api(approver, {
      "GET /api/v1/labels/plans/4": () => {
        reads += 1;
        return json(reads === 1 ? approved : applied);
      },
      "POST /api/v1/labels/plans/4/apply": () => json(applying, 202),
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderAt("/labels/plans/4");

    await user.click(await screen.findByRole("button", { name: "Apply in GitLab" }));
    expect(await screen.findByText("Plan 4 is being applied.")).toBeInTheDocument();
    expect(screen.getByText(/Applying: Being applied in GitLab/)).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(APPLY_POLL_MS);
    });

    expect(await screen.findByText(/Applied: Every step was applied/)).toBeInTheDocument();
    const rows = within(screen.getByRole("table", { name: /Plan steps/ })).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("AppliedGitLab answered 200");
    expect(rows[1]).toHaveTextContent("Read back: as intended");
    expect(rows[2]).toHaveTextContent("Already as intended");
    expect(screen.getByText("1 applied, 1 already as intended.")).toBeInTheDocument();
    expect(calls.filter((call) => call.url === "/api/v1/labels/plans/4/apply")).toHaveLength(1);
  });

  it("turns applying off while a past date is chosen", async () => {
    const AS_OF = "2026-10-01T00:00:00Z";
    api(approver, {
      [`${TAXONOMY}?as_of=${encodeURIComponent(AS_OF)}`]: () => json(taxonomyView({ as_of: AS_OF })),
      [`${DRIFT.replace("?", `?as_of=${encodeURIComponent(AS_OF)}&`)}`]: () =>
        json(driftPage([], { as_of: AS_OF })),
      "GET /api/v1/labels/plans/4": () =>
        json(labelPlan(4, { state: "approved", can_apply: { allowed: true, reasons: [] } })),
    });
    const router = renderAt(`/labels?as_of=${AS_OF}`);
    await screen.findByRole("heading", { name: "Labels" });
    await act(async () => {
      await router.navigate("/labels/plans/4");
    });

    const apply = await screen.findByRole("button", { name: "Apply in GitLab" });
    expect(apply).toBeDisabled();
    const why = document.getElementById(apply.getAttribute("aria-describedby") ?? "");
    expect(why).toHaveTextContent("You are viewing the past");
  });

  it("shows where a failed plan stopped", async () => {
    api(planner, {
      "GET /api/v1/labels/plans/4": () =>
        json(
          labelPlan(4, {
            state: "failed",
            failed_step: 2,
            failure: "GitLab answered 409",
            steps: [step(1, { status: "applied", verified: true }), step(2, { status: "failed", request_status: 409 }), step(3, { status: "not_run" })],
          }),
        ),
    });
    renderAt("/labels/plans/4");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Stopped at step 2: GitLab answered 409. Later steps were not run",
    );
    const rows = within(screen.getByRole("table", { name: /Plan steps/ })).getAllByRole("row");
    expect(rows[3]).toHaveTextContent("Not run");
  });
});
