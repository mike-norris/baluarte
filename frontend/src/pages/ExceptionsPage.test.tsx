import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FindingDetail } from "../api/evaluations";
import type { PolicyException } from "../api/exceptions";
import type { CurrentUser } from "../auth/session";
import { expiryRange } from "../exceptions/format";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { evaluation, findingDetail, observation } from "../test/findings";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const FINDING = "GET /api/v1/findings/5";
const HISTORY = "GET /api/v1/findings/5/evaluations?limit=50";
const EVIDENCE = "GET /api/v1/gitlab/projects/105/observations?before_id=10&limit=1";
const FOR_RULE =
  "GET /api/v1/exceptions?project=105&policy=corporate-default&rule=project-visibility&limit=50";
const EXCEPTION = "GET /api/v1/exceptions/31";

const requester: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "exception:request"],
};
const approver: CurrentUser = {
  ...analyst,
  user_id: 2,
  subject: "user-2",
  display_name: "Grace Hopper",
  roles: ["admin"],
  permissions: [...requester.permissions, "exception:approve", "evidence:export"],
  csrf_token: "a".repeat(64),
};
const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:export", "evidence:read", "findings:read", "inventory:read"],
};

function exception(id: number, extra: Partial<PolicyException> = {}): PolicyException {
  return {
    id,
    project: { gitlab_id: 105, path: "platform/api", state: "active" },
    policy_name: "corporate-default",
    rule_id: "project-visibility",
    justification: "Public until the docs site moves to Pages.",
    state: "requested",
    in_force: false,
    requested_by: { user_id: 1, display_name: "Ada Lovelace" },
    requested_at: "2026-10-29T08:00:00Z",
    expires_at: "2027-01-31T23:59:59Z",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    ended_by: null,
    ended_at: null,
    end_note: null,
    ...extra,
  };
}

function page(items: PolicyException[], next: number | null = null) {
  return json({ items, next_after_id: next });
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

function findingApi(user: CurrentUser, detail: FindingDetail, routes: Routes = {}): Call[] {
  return api(user, {
    [FINDING]: () => json(detail),
    [HISTORY]: () => json({ items: [detail.latest_evaluation], next_before_id: null }),
    [EVIDENCE]: () => json({ items: [observation(9)], next_before_id: 8 }),
    [FOR_RULE]: () => page([]),
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

describe("requesting an exception from a finding", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("validates the form, then sends the request with the CSRF token", async () => {
    const user = userEvent.setup();
    let requested = false;
    const calls = findingApi(requester, findingDetail(5), {
      [FOR_RULE]: () => page(requested ? [exception(31)] : []),
      "POST /api/v1/exceptions": () => {
        requested = true;
        return json(exception(31), 201);
      },
    });
    renderAt("/findings/5");

    const region = await screen.findByRole("region", { name: "Exception" });
    await user.click(await within(region).findByRole("button", { name: "Request exception" }));
    expect(screen.getByRole("heading", { name: "Request an exception" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Submit request" }));
    const problems = screen.getByRole("alert");
    expect(problems).toHaveTextContent("Give a justification of at least 10 characters.");
    expect(problems).toHaveTextContent("Choose the day the exception expires.");
    expect(problems).toHaveFocus();
    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);

    const { max } = expiryRange();
    await user.type(
      screen.getByRole("textbox", { name: "Justification" }),
      "  Public until the docs site moves to Pages.  ",
    );
    fireEvent.change(screen.getByLabelText("Expires on (UTC)"), { target: { value: max } });
    await user.click(screen.getByRole("button", { name: "Submit request" }));

    const status = await screen.findByText(
      "Exception 31 requested. It has no effect until someone else approves it.",
    );
    expect(status).toHaveFocus();
    const post = calls.find((call) => call.init?.method === "POST");
    expect(post?.url).toBe("/api/v1/exceptions");
    expect(bodyOf(post)).toEqual({
      project_gitlab_id: 105,
      policy_name: "corporate-default",
      rule_id: "project-visibility",
      justification: "Public until the docs site moves to Pages.",
      expires_at: `${max}T23:59:59Z`,
    });
    expect(headerOf(post, "X-CSRF-Token")).toBe(requester.csrf_token);
    expect(
      await within(region).findByText(
        "An exception is already requested for this rule; a new one can be requested after it ends.",
      ),
    ).toBeInTheDocument();
    expect(within(region).getByRole("link", { name: "Exception 31" })).toHaveAttribute(
      "href",
      "/exceptions/31",
    );
  });

  it("shows the API's reason when the request is refused", async () => {
    const user = userEvent.setup();
    findingApi(requester, findingDetail(5), {
      "POST /api/v1/exceptions": () =>
        json(
          { detail: "Exception 3 is already requested or approved for this project and rule" },
          409,
        ),
    });
    renderAt("/findings/5");

    await user.click(await screen.findByRole("button", { name: "Request exception" }));
    await user.type(
      screen.getByRole("textbox", { name: "Justification" }),
      "A justification long enough.",
    );
    fireEvent.change(screen.getByLabelText("Expires on (UTC)"), {
      target: { value: expiryRange().min },
    });
    await user.click(screen.getByRole("button", { name: "Submit request" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Not possible now: Exception 3 is already requested or approved for this project and rule.",
    );
  });

  it("explains that an unknown result cannot be excepted", async () => {
    findingApi(
      requester,
      findingDetail(
        5,
        evaluation(20, {
          outcome: "unknown",
          observed: null,
          observation_id: null,
          unknown_reasons: ["collection_stale"],
        }),
      ),
    );
    renderAt("/findings/5");

    const region = await screen.findByRole("region", { name: "Exception" });
    expect(
      await within(region).findByText(/An exception waives a known failure only/),
    ).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Request exception" })).toBeNull();
  });

  it("offers no request to users without exception:request", async () => {
    findingApi(auditor, findingDetail(5));
    renderAt("/findings/5");

    const region = await screen.findByRole("region", { name: "Exception" });
    expect(
      await within(region).findByText("No exception is requested or in force for this rule."),
    ).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Request exception" })).toBeNull();
  });

  it("shows an excepted finding's expiry and that it is not passing", async () => {
    findingApi(
      requester,
      findingDetail(5, evaluation(20, { outcome: "excepted", exception_id: 31 }), {
        state: "excepted",
        exception: { id: 31, expires_at: "2027-01-31T23:59:59Z", in_force: true },
      }),
      {
        [FOR_RULE]: () =>
          page([
            exception(31, {
              state: "approved",
              in_force: true,
              decided_by: { user_id: 2, display_name: "Grace Hopper" },
              decided_at: "2026-10-29T09:00:00Z",
            }),
          ]),
      },
    );
    renderAt("/findings/5");

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(summary).toHaveTextContent(
      "Excepted until 31 Jan 2027, 23:59 UTC (not counted as passing)",
    );
    const region = screen.getByRole("region", { name: "Exception" });
    expect(region).toHaveTextContent(
      "Excepted until 31 Jan 2027, 23:59 UTC: the failure is recorded as excepted, not as passing.",
    );
    expect(await within(region).findByText(/By Grace Hopper on/)).toBeInTheDocument();
    expect(region).toHaveTextContent("Approved: In force");
  });
});

describe("exceptions list", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists exceptions, pages, and keeps filters in the URL", async () => {
    const user = userEvent.setup();
    api(requester, {
      "GET /api/v1/exceptions?state=approved&limit=50": () =>
        page(
          [
            exception(1, { state: "approved", in_force: true }),
            exception(2, { state: "approved", in_force: true }),
          ],
          2,
        ),
      "GET /api/v1/exceptions?state=approved&after_id=2&limit=50": () =>
        page([exception(3, { state: "approved", in_force: true })]),
      "GET /api/v1/exceptions?state=expired&project=105&limit=50": () => page([]),
    });
    const router = renderAt("/exceptions?state=approved");

    const table = await screen.findByRole("table", { name: "Policy exceptions" });
    const row = within(table).getByRole("rowheader", { name: "Exception 1" }).closest("tr");
    expect(row).toHaveTextContent("platform/api");
    expect(row).toHaveTextContent("corporate-default / project-visibility");
    expect(row).toHaveTextContent("Approved(in force)");
    expect(row).toHaveTextContent("Ada Lovelace");
    expect(row).toHaveTextContent("31 Jan 2027, 23:59 UTC");
    expect(screen.getByText("Showing 2 exceptions (more available).")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more exceptions" }));
    expect(await screen.findByRole("rowheader", { name: "Exception 3" })).toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "State" }), "expired");
    await user.type(screen.getByRole("spinbutton", { name: "Project GitLab ID" }), "105");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("No exceptions match these filters.")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?state=expired&project=105");
  });
});

describe("deciding an exception", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("approves after confirmation, with the CSRF token, and records the history", async () => {
    const user = userEvent.setup();
    const calls = api(approver, {
      [EXCEPTION]: () => json(exception(31)),
      "POST /api/v1/exceptions/31/approve": () =>
        json(
          exception(31, {
            state: "approved",
            in_force: true,
            decided_by: { user_id: 2, display_name: "Grace Hopper" },
            decided_at: "2026-10-29T09:00:00Z",
          }),
        ),
    });
    renderAt("/exceptions/31");

    const decision = await screen.findByRole("region", { name: "Decision" });
    await user.click(within(decision).getByRole("button", { name: "Approve…" }));
    expect(screen.getByRole("heading", { name: "Approve this exception?" })).toHaveFocus();
    expect(decision).toHaveTextContent("This change is audited.");
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    const status = await screen.findByText(/^Exception 31 approved\./);
    expect(status).toHaveFocus();
    const post = calls.find((call) => call.init?.method === "POST");
    expect(post?.url).toBe("/api/v1/exceptions/31/approve");
    expect(bodyOf(post)).toEqual({});
    expect(headerOf(post, "X-CSRF-Token")).toBe(approver.csrf_token);
    expect(screen.getByRole("region", { name: "Summary" })).toHaveTextContent("Approved: In force");
    const steps = within(screen.getByRole("region", { name: "History" })).getAllByRole("listitem");
    expect(steps.map((step) => step.querySelector("span")?.textContent)).toEqual([
      "Requested",
      "Approved",
    ]);
    expect(steps[1]).toHaveTextContent("by Grace Hopper");
  });

  it("hides approve and reject on the user's own request, by user ID", async () => {
    // The request is user 1's; this approver is user 1 under another display name.
    api({ ...approver, user_id: 1 }, { [EXCEPTION]: () => json(exception(31)) });
    renderAt("/exceptions/31");

    const decision = await screen.findByRole("region", { name: "Decision" });
    expect(decision).toHaveTextContent(
      "This is your own request. Someone else must approve or reject it.",
    );
    expect(within(decision).queryByRole("button", { name: "Approve…" })).toBeNull();
    expect(within(decision).queryByRole("button", { name: "Reject…" })).toBeNull();
    expect(within(decision).getByRole("button", { name: "Revoke…" })).toBeInTheDocument();
  });

  it("offers approval to another user who shares the requester's display name", async () => {
    api({ ...approver, display_name: "Ada Lovelace" }, { [EXCEPTION]: () => json(exception(31)) });
    renderAt("/exceptions/31");

    const decision = await screen.findByRole("region", { name: "Decision" });
    expect(within(decision).getByRole("button", { name: "Approve…" })).toBeInTheDocument();
    expect(decision).not.toHaveTextContent("This is your own request.");
  });

  it("shows the API's separation-of-duties refusal", async () => {
    const user = userEvent.setup();
    api(approver, {
      [EXCEPTION]: () => json(exception(31)),
      "POST /api/v1/exceptions/31/approve": () =>
        json({ detail: "An exception must be approved or rejected by someone else" }, 403),
    });
    renderAt("/exceptions/31");

    await user.click(await screen.findByRole("button", { name: "Approve…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Refused: An exception must be approved or rejected by someone else.",
    );
    expect(alert).toHaveFocus();
  });

  it("requires a reason to reject, and sends it", async () => {
    const user = userEvent.setup();
    const calls = api(approver, {
      [EXCEPTION]: () => json(exception(31)),
      "POST /api/v1/exceptions/31/reject": () =>
        json(
          exception(31, {
            state: "rejected",
            decided_by: { user_id: 2, display_name: "Grace Hopper" },
            decided_at: "2026-10-29T09:00:00Z",
            decision_note: "Move the docs first.",
          }),
        ),
    });
    renderAt("/exceptions/31");

    await user.click(await screen.findByRole("button", { name: "Reject…" }));
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Give a reason");
    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);

    await user.type(screen.getByRole("textbox", { name: "Reason" }), "Move the docs first.");
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));

    expect(await screen.findByText(/^Exception 31 rejected\./)).toBeInTheDocument();
    expect(bodyOf(calls.find((call) => call.init?.method === "POST"))).toEqual({
      note: "Move the docs first.",
    });
    expect(screen.getByRole("region", { name: "History" })).toHaveTextContent(
      "Move the docs first.",
    );
  });

  it("offers no decisions without exception:approve", async () => {
    api(requester, { [EXCEPTION]: () => json(exception(31)) });
    renderAt("/exceptions/31");

    expect(await screen.findByRole("region", { name: "Summary" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Decision" })).toBeNull();
  });

  it("lists a revoked exception's history oldest first", async () => {
    api(requester, {
      [EXCEPTION]: () =>
        json(
          exception(31, {
            state: "revoked",
            decided_by: { user_id: 2, display_name: "Grace Hopper" },
            decided_at: "2026-10-29T09:00:00Z",
            ended_by: { user_id: 3, display_name: "Alan Turing" },
            ended_at: "2026-11-01T10:00:00Z",
            end_note: "The docs moved.",
          }),
        ),
    });
    renderAt("/exceptions/31");

    const history = await screen.findByRole("region", { name: "History" });
    const steps = within(history).getAllByRole("listitem");
    expect(steps.map((step) => step.querySelector("span")?.textContent)).toEqual([
      "Requested",
      "Approved",
      "Revoked",
    ]);
    expect(steps[2]).toHaveTextContent("1 Nov 2026, 10:00 UTC by Alan Turing");
    expect(steps[2]).toHaveTextContent("The docs moved.");
  });

  it("shows an expired exception as expired", async () => {
    api(requester, {
      [EXCEPTION]: () =>
        json(
          exception(31, {
            state: "expired",
            decided_by: { user_id: 2, display_name: "Grace Hopper" },
            decided_at: "2026-10-29T09:00:00Z",
            expires_at: "2026-10-30T23:59:59Z",
          }),
        ),
    });
    renderAt("/exceptions/31");

    expect(await screen.findByRole("region", { name: "Summary" })).toHaveTextContent(
      "Expired: Past its expiry.",
    );
    const steps = within(screen.getByRole("region", { name: "History" })).getAllByRole("listitem");
    expect(steps.at(-1)).toHaveTextContent("Expired 30 Oct 2026, 23:59 UTC");
  });

  it("says when there is no such exception", async () => {
    api(requester, {
      "GET /api/v1/exceptions/99": () => json({ detail: "No such exception" }, 404),
    });
    renderAt("/exceptions/99");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Exception not found" }),
    ).toBeInTheDocument();
  });
});
