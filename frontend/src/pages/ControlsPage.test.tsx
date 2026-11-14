import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ControlDetail } from "../api/controls";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import {
  CONTROLS_SOC2,
  PENDING_MAPPING,
  control,
  controlDetail,
  controlList,
  mappingChange,
} from "../test/controls";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const DETAIL = "GET /api/v1/controls/soc2/CC8.1";
const PROPOSE = "POST /api/v1/control-mapping/changes";

const auditor: CurrentUser = {
  ...analyst,
  user_id: 3,
  roles: ["auditor"],
  permissions: ["evidence:read", "findings:read", "inventory:read"],
};

function api(
  extra: Record<string, (init?: RequestInit) => Response> = {},
  user: CurrentUser = analyst,
): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [PENDING_MAPPING]: () => json({ items: [] }),
    ...extra,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function bodyOf(calls: Call[], key: string): unknown {
  const call = calls.find((item) => `${item.init?.method ?? "GET"} ${item.url}` === key);
  return JSON.parse(String(call?.init?.body));
}

const list = controlList([
  control("CC8.1", {
    status: "not_met",
    counts: { pass: 9, fail: 3, unknown: 1, excepted: 0, projects: 13 },
  }),
  control("CC6.8", {
    title: "Malicious software is prevented or detected",
    area: "Logical access",
    status: "not_covered",
    mapped: [],
    counts: { pass: 0, fail: 0, unknown: 0, excepted: 0, projects: 0 },
  }),
  control("CC6.1", {
    title: "Logical access is restricted",
    status: "manual",
    mapped: [
      {
        rule_type: "gitlab.project.access_tokens",
        source: "organization",
        change_id: 8,
        justification: "Token lifetime is part of acme's access standard.",
      },
    ],
  }),
]);

describe("controls page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists a framework's controls with status text, mapped rules and results", async () => {
    api({ [CONTROLS_SOC2]: () => json(list) });
    renderAt("/controls");

    const table = await screen.findByRole("table", { name: "SOC 2 controls" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("CC8.1");
    expect(rows[0]).toHaveTextContent("Not met");
    expect(rows[0]).toHaveTextContent("Default branch protected");
    expect(rows[0]).toHaveTextContent("Built in");
    expect(rows[0]).toHaveTextContent("9 pass, 3 fail, 1 unknown");
    expect(rows[1]).toHaveTextContent("Not covered");
    expect(rows[1]).toHaveTextContent("None");
    expect(rows[2]).toHaveTextContent("Met outside GitLab");
    expect(rows[2]).toHaveTextContent("Organization");
    expect(within(rows[0] as HTMLElement).getByRole("link", { name: "CC8.1" })).toHaveAttribute(
      "href",
      "/controls/soc2/CC8.1",
    );
    expect(screen.getByText(/1 not met, 1 not covered, 1 met outside gitlab/)).toHaveTextContent(
      "mapping version 2026.11",
    );
    expect(screen.getByText(/It is not a certification/)).toBeInTheDocument();
  });

  it("filters by status and switches framework", async () => {
    const user = userEvent.setup();
    const calls = api({
      [CONTROLS_SOC2]: () => json(list),
      "GET /api/v1/controls?framework=iso27001": () =>
        json(
          controlList([control("A.8.32", { framework: "iso27001", title: "Changes are managed" })]),
        ),
    });
    const router = renderAt("/controls");

    await screen.findByRole("table", { name: "SOC 2 controls" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "not_covered");
    expect(router.state.location.search).toBe("?status=not_covered");
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("CC6.8");

    await user.click(screen.getByRole("button", { name: "ISO 27001" }));
    expect(router.state.location.search).toBe("?framework=iso27001");
    expect(await screen.findByRole("table", { name: "ISO 27001 controls" })).toHaveTextContent(
      "A.8.32",
    );
    expect(screen.getByRole("button", { name: "ISO 27001" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(calls.some((call) => call.url === "/api/v1/controls?framework=iso27001")).toBe(true);
  });

  it("says when mapping changes wait for review", async () => {
    api({
      [CONTROLS_SOC2]: () => json(list),
      [PENDING_MAPPING]: () => json({ items: [mappingChange(4)] }),
    });
    renderAt("/controls");

    expect(
      await screen.findByText("1 mapping change is waiting for review.", { exact: false }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review changes" })).toHaveAttribute(
      "href",
      "/governance/changes",
    );
  });

  it("shows the API's error", async () => {
    api({ [CONTROLS_SOC2]: () => json({ detail: "x" }, 500) });
    renderAt("/controls");

    expect(await screen.findByRole("alert")).toHaveTextContent("The API returned an error (500).");
  });
});

describe("control detail", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const detail: ControlDetail = controlDetail({
    changes: [
      mappingChange(2, {
        kind: "map",
        state: "rejected",
        rule_type: "gitlab.project.visibility",
        decided_by: { actor: "user:2", user_id: 2, display_name: "Grace Hopper" },
        decided_at: "2026-11-02T10:00:00Z",
        decision_note: "Visibility is not a change control.",
      }),
    ],
  });

  it("shows the status, mapped rules, results with links, and history", async () => {
    api({ [DETAIL]: () => json(detail) });
    renderAt("/controls/soc2/CC8.1");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "SOC 2 CC8.1: Changes are authorized, tested and approved",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("At least one result of a mapped rule fails.")).toBeInTheDocument();
    const mapped = screen.getByRole("list", { name: "Mapped rule types" });
    expect(mapped).toHaveTextContent("Default branch protected");
    expect(mapped).toHaveTextContent("Protected branches stop changes that skip review.");
    const results = screen.getByRole("table", { name: "Results per rule" });
    expect(results).toHaveTextContent("corporate-default / default-branch-protected");
    expect(
      within(results).getByRole("link", { name: "Findings for default-branch-protected" }),
    ).toHaveAttribute("href", "/findings?policy=corporate-default&rule=default-branch-protected");
    const history = screen.getByRole("table", { name: "Mapping changes" });
    expect(history).toHaveTextContent("Count Project visibility toward SOC 2 CC8.1");
    expect(history).toHaveTextContent("Rejected by Grace Hopper");
    expect(history).toHaveTextContent("“Visibility is not a change control.”");
    expect(screen.getByRole("link", { name: "Export evidence for this control" })).toHaveAttribute(
      "href",
      "/evidence?framework=soc2&control=CC8.1",
    );
  });

  it("offers no changes to users without policy:write", async () => {
    api({ [DETAIL]: () => json(detail) }, auditor);
    renderAt("/controls/soc2/CC8.1");

    await screen.findByRole("list", { name: "Mapped rule types" });
    expect(screen.queryByRole("heading", { name: "Adjust the mapping" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
  });

  it("maps a rule type with a justification and says it waits for review", async () => {
    const user = userEvent.setup();
    let calls: Call[] = [];
    let proposed = false;
    calls = api({
      [DETAIL]: () =>
        json(proposed ? { ...detail, changes: [mappingChange(9), ...detail.changes] } : detail),
      [PROPOSE]: () => {
        proposed = true;
        return json(mappingChange(9), 201);
      },
    });
    renderAt("/controls/soc2/CC8.1");

    await user.click(await screen.findByRole("button", { name: "Map a rule type" }));
    const form = screen.getByRole("form", { name: "Map a rule type: SOC 2 CC8.1" });
    const select = within(form).getByRole("combobox", { name: "Rule type" });
    expect(
      within(select).queryByRole("option", { name: /Default branch protected/ }),
    ).not.toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Propose change" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("Choose a rule type.");
    expect(within(form).getByRole("alert")).toHaveTextContent(
      "Justification: explain the reason in at least 20 characters.",
    );
    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);

    await user.selectOptions(select, "gitlab.project.merge_request_approvals");
    await user.type(
      within(form).getByLabelText("Justification"),
      "Project approval settings are how acme enforces peer review.",
    );
    await user.click(within(form).getByRole("button", { name: "Propose change" }));

    expect(
      await screen.findByText(
        "Proposed for review: Count Project approval settings toward SOC 2 CC8.1. It takes effect once someone other than you approves it.",
      ),
    ).toBeInTheDocument();
    expect(bodyOf(calls, PROPOSE)).toEqual({
      framework: "soc2",
      control_id: "CC8.1",
      kind: "map",
      rule_type: "gitlab.project.merge_request_approvals",
      justification: "Project approval settings are how acme enforces peer review.",
    });
    const sent = calls.find((call) => call.init?.method === "POST");
    expect(sent?.init?.headers).toMatchObject({ "X-CSRF-Token": analyst.csrf_token });
    expect(
      await screen.findByText("1 change to this control is waiting for review.", { exact: false }),
    ).toBeInTheDocument();
  });

  it("removes a built-in pair and shows the API's reason when it refuses", async () => {
    const user = userEvent.setup();
    const calls = api({
      [DETAIL]: () => json(detail),
      [PROPOSE]: () => json({ detail: { code: "duplicate" } }, 409),
    });
    renderAt("/controls/soc2/CC8.1");

    await user.click(
      await screen.findByRole("button", { name: "Remove Default branch protected" }),
    );
    const form = screen.getByRole("form", { name: "Remove a built-in mapping: SOC 2 CC8.1" });
    expect(form).toHaveTextContent("Rule type: Default branch protected");
    await user.type(
      within(form).getByLabelText("Justification"),
      "acme protects branches with a server-side hook instead.",
    );
    await user.click(within(form).getByRole("button", { name: "Propose change" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Not possible now: the same change is already in force or waiting for review.",
    );
    expect(bodyOf(calls, PROPOSE)).toMatchObject({
      kind: "unmap",
      rule_type: "gitlab.branch.default_protected",
    });
  });

  it("marks a control not applicable only with a review date, applied at once when review is off", async () => {
    const user = userEvent.setup();
    const uncovered = controlDetail({
      ...control("CC6.8", { status: "not_covered", mapped: [] }),
      rules: [],
      changes: [],
    });
    const calls = api({
      "GET /api/v1/controls/soc2/CC6.8": () => json(uncovered),
      [PROPOSE]: () =>
        json(
          mappingChange(10, {
            kind: "not_applicable",
            control_id: "CC6.8",
            rule_type: null,
            state: "active",
            self_approved: true,
          }),
          201,
        ),
    });
    renderAt("/controls/soc2/CC6.8");

    expect(
      await screen.findByText(/No changes: the built-in mapping applies as published/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mark not applicable" }));
    const form = screen.getByRole("form", { name: "Mark not applicable: SOC 2 CC6.8" });
    await user.type(
      within(form).getByLabelText("Justification"),
      "acme ships no executables from GitLab; endpoint tooling covers malware.",
    );
    await user.click(within(form).getByRole("button", { name: "Propose change" }));
    expect(within(form).getByRole("alert")).toHaveTextContent(
      "Review by: choose the date this decision must be reviewed again.",
    );

    const tomorrow = new Date(Date.now() + 2 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
    await user.type(within(form).getByLabelText("Review by"), tomorrow);
    await user.type(within(form).getByLabelText(/Where the evidence is/), "Endpoint policy EP-2");
    await user.click(within(form).getByRole("button", { name: "Propose change" }));

    expect(
      await screen.findByText(
        "Applied: Mark SOC 2 CC6.8 not applicable. Review was off, so it is in force now.",
      ),
    ).toBeInTheDocument();
    expect(bodyOf(calls, PROPOSE)).toEqual({
      framework: "soc2",
      control_id: "CC6.8",
      kind: "not_applicable",
      justification: "acme ships no executables from GitLab; endpoint tooling covers malware.",
      evidence_ref: "Endpoint policy EP-2",
      review_by: tomorrow,
    });
  });

  it("shows an organization decision and withdraws it", async () => {
    const user = userEvent.setup();
    const manual = controlDetail({
      ...control("CC6.1", {
        status: "manual",
        mapped: [],
        change: {
          id: 12,
          kind: "manual",
          justification: "Quarterly access reviews in the identity provider.",
          evidence_ref: "Access review records",
          review_by: "2027-06-30",
        },
      }),
      rules: [],
      changes: [],
    });
    const calls = api({
      "GET /api/v1/controls/soc2/CC6.1": () => json(manual),
      [PROPOSE]: () =>
        json(
          mappingChange(13, {
            kind: "withdraw",
            control_id: "CC6.1",
            supersedes_id: 12,
            rule_type: null,
          }),
          201,
        ),
    });
    renderAt("/controls/soc2/CC6.1");

    const decision = await screen.findByRole("region", { name: "Met outside GitLab" });
    expect(decision).toHaveTextContent("Quarterly access reviews in the identity provider.");
    expect(decision).toHaveTextContent("Access review records");
    expect(decision).toHaveTextContent("2027-06-30");
    expect(screen.queryByRole("button", { name: "Mark not applicable" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Withdraw “met outside GitLab”" }));
    const form = screen.getByRole("form", { name: "Withdraw a change: SOC 2 CC6.1" });
    expect(form).toHaveTextContent(
      "Withdraws change 12: records the control as met outside GitLab.",
    );
    await user.type(
      within(form).getByLabelText("Justification"),
      "Access reviews moved to a tool Baluarte now checks.",
    );
    await user.click(within(form).getByRole("button", { name: "Propose change" }));

    expect(await screen.findByText(/Proposed for review: Withdraw change 12/)).toBeInTheDocument();
    expect(bodyOf(calls, PROPOSE)).toMatchObject({ kind: "withdraw", supersedes_id: 12 });
  });

  it("restores a built-in pair the organization removed", async () => {
    const user = userEvent.setup();
    const removed = controlDetail({
      ...control("CC6.6", {
        status: "not_covered",
        mapped: [],
        unmapped: [
          {
            rule_type: "gitlab.project.visibility",
            change_id: 21,
            justification: "Visibility is governed by the network boundary review instead.",
          },
        ],
      }),
      rules: [],
      changes: [],
    });
    const calls = api({
      "GET /api/v1/controls/soc2/CC6.6": () => json(removed),
      [PROPOSE]: () =>
        json(
          mappingChange(22, {
            kind: "withdraw",
            control_id: "CC6.6",
            supersedes_id: 21,
            rule_type: null,
          }),
          201,
        ),
    });
    renderAt("/controls/soc2/CC6.6");

    const list = await screen.findByRole("list", { name: "Removed rule types" });
    expect(list).toHaveTextContent(
      "Visibility is governed by the network boundary review instead.",
    );
    await user.click(within(list).getByRole("button", { name: /^Restore / }));
    const form = screen.getByRole("form", { name: "Withdraw a change: SOC 2 CC6.6" });
    expect(form).toHaveTextContent(/Withdraws change 21: removed .* from the control\./);
    await user.type(
      within(form).getByLabelText("Justification"),
      "The boundary review no longer covers repository visibility.",
    );
    await user.click(within(form).getByRole("button", { name: "Propose change" }));

    expect(await screen.findByText(/Withdraw change 21/)).toBeInTheDocument();
    expect(bodyOf(calls, PROPOSE)).toMatchObject({ kind: "withdraw", supersedes_id: 21 });
  });

  it("explains an unknown control", async () => {
    api({ [DETAIL]: () => json({ detail: "Control not found" }, 404) });
    renderAt("/controls/soc2/CC8.1");

    expect(await screen.findByRole("alert")).toHaveTextContent("The API returned an error (404).");
    expect(screen.getByRole("link", { name: "All controls" })).toHaveAttribute("href", "/controls");
  });
});
