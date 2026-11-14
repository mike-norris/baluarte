import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PolicyChange, PolicyChangeDetail, PolicyChangePage } from "../api/policies";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { PENDING_MAPPING, mappingChange } from "../test/controls";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { SCOPE_PROPOSED, exclusion, exclusions } from "../test/scope";

const PENDING = "GET /api/v1/policy-changes?state=proposed&limit=50";
const HISTORY = "GET /api/v1/policy-changes?limit=50";

const reviewer: CurrentUser = { ...analyst, user_id: 2, display_name: "Grace Hopper" };

const versionChange: PolicyChange = {
  id: 5,
  kind: "version",
  state: "proposed",
  policy_name: "default-gitlab-baseline",
  version: null,
  yaml_digest: "a".repeat(64),
  base_version: 1,
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
};

const assignChange: PolicyChange = {
  ...versionChange,
  id: 6,
  kind: "assign",
  version: 1,
  yaml_digest: null,
  base_version: null,
  target_type: "group",
  target_id: 10,
  target_path: "platform",
};

const detail: PolicyChangeDetail = {
  ...versionChange,
  source_yaml: "rules:\n  allowed: [private]\n",
  base_source_yaml: "rules:\n  allowed: [private, internal]\n",
};

const decided: PolicyChange = {
  ...assignChange,
  id: 4,
  state: "approved",
  review_required: false,
  self_approved: true,
  decided_by: versionChange.proposed_by,
  decided_at: "2026-10-31T10:00:00Z",
};

function page(items: PolicyChange[], reviewRequired = true): PolicyChangePage {
  return { review_required: reviewRequired, items, next_before_id: null };
}

function api(user: CurrentUser, extra: Record<string, (init?: RequestInit) => Response> = {}) {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [PENDING]: () => json(page([versionChange, assignChange])),
    [HISTORY]: () => json(page([assignChange, versionChange, decided])),
    "GET /api/v1/policy-changes/5": () => json(detail),
    [PENDING_MAPPING]: () => json({ items: [] }),
    [SCOPE_PROPOSED]: () => json(exclusions([])),
    ...extra,
  });
}

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function sent(calls: Call[], method: string): Call[] {
  return calls.filter((call) => call.init?.method === method);
}

describe("baseline changes", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists pending changes with what they change and who proposed them", async () => {
    api(reviewer);
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", { name: "Changes waiting for review" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("New version of default-gitlab-baseline (replaces v1)");
    expect(items[0]).toHaveTextContent("Proposed by Ada Lovelace");
    const diff = await within(items[0] as HTMLElement).findByLabelText(
      "Changes from v1, the version in force",
    );
    expect(diff).toHaveTextContent("- allowed: [private, internal]");
    expect(diff).toHaveTextContent("+ allowed: [private]");
    expect(items[1]).toHaveTextContent("Apply default-gitlab-baseline v1 to group platform");
    expect(
      screen.getByText("2 baseline changes are waiting for review.", { exact: false }),
    ).toBeInTheDocument();
    const history = screen.getByRole("table", { name: "Decided baseline changes" });
    expect(within(history).getAllByRole("row")).toHaveLength(2);
    expect(history).toHaveTextContent("Not reviewed: review was off, so it applied at once");
  });

  it("does not let the proposer approve or reject their own change", async () => {
    api(analyst);
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", { name: "Changes waiting for review" });
    const first = within(list).getAllByRole("listitem")[0] as HTMLElement;
    expect(within(first).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(first).getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(first).toHaveTextContent(
      "You proposed this change, so someone else must approve or reject it.",
    );
  });

  it("approves a change and reloads the list", async () => {
    let approved = false;
    const calls = api(reviewer, {
      [PENDING]: () => json(page(approved ? [assignChange] : [versionChange, assignChange])),
      "POST /api/v1/policy-changes/5/approve": () => {
        approved = true;
        return json({ ...detail, state: "approved", version: 2 });
      },
    });
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", { name: "Changes waiting for review" });
    const first = within(list).getAllByRole("listitem")[0] as HTMLElement;
    await user.type(within(first).getByLabelText("Note (required to reject)"), "Looks right");
    await user.click(within(first).getByRole("button", { name: "Approve" }));

    expect(
      await screen.findByText(
        "Approved: New version of default-gitlab-baseline (replaces v1). It is in force now.",
      ),
    ).toBeInTheDocument();
    const [post] = sent(calls, "POST");
    expect(post?.init?.body).toBe(JSON.stringify({ note: "Looks right" }));
    expect(
      within(await screen.findByRole("list", { name: "Changes waiting for review" })).getAllByRole(
        "listitem",
      ),
    ).toHaveLength(1);
  });

  it("asks for a note before rejecting", async () => {
    const calls = api(reviewer);
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", { name: "Changes waiting for review" });
    const second = within(list).getAllByRole("listitem")[1] as HTMLElement;
    await user.click(within(second).getByRole("button", { name: "Reject" }));

    expect(within(second).getByRole("alert")).toHaveTextContent(
      "Explain why in the note before rejecting.",
    );
    expect(sent(calls, "POST")).toEqual([]);
  });

  it("warns on every Governance view when changes take effect without review", async () => {
    api(reviewer, { [PENDING]: () => json(page([], false)) });
    renderAt("/governance/changes");

    expect(
      await screen.findByText("Baseline changes take effect without review."),
    ).toBeInTheDocument();
    expect(await screen.findByText("Nothing is waiting for review.")).toBeInTheDocument();
    expect(await screen.findByText("No control mapping change is waiting.")).toBeInTheDocument();
  });
});

describe("control mapping changes in the review queue", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const manual = mappingChange(7, {
    kind: "manual",
    control_id: "CC6.1",
    rule_type: null,
    evidence_ref: "Access review records",
    review_by: "2027-06-30",
    justification: "Quarterly access reviews in the identity provider.",
  });

  it("lists proposed mapping changes with their justification beside baseline changes", async () => {
    api(reviewer, { [PENDING_MAPPING]: () => json({ items: [mappingChange(6), manual] }) });
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", {
      name: "Control mapping changes waiting for review",
    });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Count Project approval settings toward SOC 2 CC8.1");
    expect(items[0]).toHaveTextContent(
      "Project approval settings are how acme enforces peer review.",
    );
    expect(items[0]).toHaveTextContent("Proposed by Ada Lovelace");
    expect(
      within(items[0] as HTMLElement).getByRole("link", { name: "View CC8.1" }),
    ).toHaveAttribute("href", "/controls/soc2/CC8.1");
    expect(items[1]).toHaveTextContent("Record SOC 2 CC6.1 as met outside GitLab");
    expect(items[1]).toHaveTextContent("Access review records");
    expect(items[1]).toHaveTextContent("2027-06-30");
    expect(screen.getByRole("list", { name: "Changes waiting for review" })).toBeInTheDocument();
  });

  it("does not let the proposer decide their own mapping change", async () => {
    api(analyst, { [PENDING_MAPPING]: () => json({ items: [mappingChange(6)] }) });
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", {
      name: "Control mapping changes waiting for review",
    });
    const item = within(list).getByRole("listitem");
    expect(within(item).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(item).getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(item).toHaveTextContent(
      "You proposed this change, so someone else must approve or reject it.",
    );
  });

  it("requires a note to reject, then rejects and reloads", async () => {
    let rejected = false;
    const calls = api(reviewer, {
      [PENDING_MAPPING]: () => json({ items: rejected ? [] : [manual] }),
      "POST /api/v1/control-mapping/changes/7/reject": () => {
        rejected = true;
        return json({ ...manual, state: "rejected", decision_note: "Needs evidence." });
      },
    });
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", {
      name: "Control mapping changes waiting for review",
    });
    const item = within(list).getByRole("listitem");
    await user.click(within(item).getByRole("button", { name: "Reject" }));
    expect(within(item).getByRole("alert")).toHaveTextContent(
      "Explain why in the note before rejecting.",
    );
    expect(sent(calls, "POST")).toEqual([]);

    await user.type(within(item).getByLabelText("Note (required to reject)"), "Needs evidence.");
    await user.click(within(item).getByRole("button", { name: "Reject" }));

    expect(
      await screen.findByText(
        "Rejected: Record SOC 2 CC6.1 as met outside GitLab. The mapping did not change.",
      ),
    ).toBeInTheDocument();
    const [post] = sent(calls, "POST");
    expect(post?.url).toBe("/api/v1/control-mapping/changes/7/reject");
    expect(post?.init?.body).toBe(JSON.stringify({ note: "Needs evidence." }));
    expect(await screen.findByText("No control mapping change is waiting.")).toBeInTheDocument();
  });

  it("approves and shows the API's refusal when it changed meanwhile", async () => {
    const calls = api(reviewer, {
      [PENDING_MAPPING]: () => json({ items: [mappingChange(6)] }),
      "POST /api/v1/control-mapping/changes/6/approve": () =>
        json({ detail: { code: "not_proposed" } }, 409),
    });
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", {
      name: "Control mapping changes waiting for review",
    });
    const item = within(list).getByRole("listitem");
    await user.click(within(item).getByRole("button", { name: "Approve" }));

    expect(await within(item).findByRole("alert")).toHaveTextContent(
      "Not possible now: the change was already decided.",
    );
    const [post] = sent(calls, "POST");
    expect(post?.init?.body).toBe(JSON.stringify({}));
  });
});

describe("exclusions from monitoring in the review queue", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const batchId = "6f1c2a3e-0000-4000-8000-00000000000b";
  const waiting = { state: "proposed", batch_id: batchId, review_required: true } as const;
  const proposedBatch = [
    exclusion(21, { ...waiting, self_approved: false }),
    exclusion(22, {
      ...waiting,
      self_approved: false,
      target_type: "group",
      target_id: 300,
      target_path: "acme/sandbox",
    }),
  ];

  async function batchItem(): Promise<HTMLElement> {
    const list = await screen.findByRole("list", {
      name: "Exclusions from monitoring waiting for review",
    });
    // The first list item is the batch; its targets are nested items.
    return within(list).getAllByRole("listitem")[0] as HTMLElement;
  }

  it("lists a proposed batch with its targets and justification", async () => {
    api(reviewer, { [SCOPE_PROPOSED]: () => json(exclusions(proposedBatch, true)) });
    renderAt("/governance/changes");

    const item = await batchItem();
    expect(item).toHaveTextContent("Exclude from monitoring: 2 targets");
    expect(within(item).getByRole("list", { name: "Targets" })).toHaveTextContent(
      "Project acme/research/prototype",
    );
    expect(item).toHaveTextContent("Group acme/sandbox");
    expect(item).toHaveTextContent("Research prototypes are out of scope for the acme audit.");
    expect(item).toHaveTextContent("Proposed by Ada Lovelace");
  });

  it("does not let the proposer decide their own batch", async () => {
    api(analyst, { [SCOPE_PROPOSED]: () => json(exclusions(proposedBatch, true)) });
    renderAt("/governance/changes");

    const item = await batchItem();
    expect(within(item).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(item).getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(item).toHaveTextContent(
      "You proposed these exclusions, so someone else must approve or reject them.",
    );
  });

  it("requires a note to reject a batch, then rejects it", async () => {
    let rejected = false;
    const calls = api(reviewer, {
      [SCOPE_PROPOSED]: () => json(exclusions(rejected ? [] : proposedBatch, true)),
      [`POST /api/v1/scope/exclusions/batches/${batchId}/reject`]: () => {
        rejected = true;
        return json({ batch_id: batchId, items: [] });
      },
    });
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const item = await batchItem();
    await user.click(within(item).getByRole("button", { name: "Reject" }));
    expect(within(item).getByRole("alert")).toHaveTextContent(
      "Explain why in the note before rejecting.",
    );
    expect(sent(calls, "POST")).toEqual([]);

    await user.type(within(item).getByLabelText("Note (required to reject)"), "Keep the sandbox.");
    await user.click(within(item).getByRole("button", { name: "Reject" }));

    expect(
      await screen.findByText(
        "Rejected: Exclude from monitoring: 2 targets. Monitoring did not change.",
      ),
    ).toBeInTheDocument();
    const [post] = sent(calls, "POST");
    expect(post?.init?.body).toBe(JSON.stringify({ note: "Keep the sandbox." }));
    expect(await screen.findByText("No exclusion from monitoring is waiting.")).toBeInTheDocument();
  });

  it("approves a batch and explains a refusal", async () => {
    const calls = api(reviewer, {
      [SCOPE_PROPOSED]: () => json(exclusions(proposedBatch, true)),
      [`POST /api/v1/scope/exclusions/batches/${batchId}/approve`]: () =>
        json({ detail: { code: "not_proposed", message: "Already decided", reasons: [] } }, 409),
    });
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const item = await batchItem();
    await user.click(within(item).getByRole("button", { name: "Approve" }));

    expect(await within(item).findByRole("alert")).toHaveTextContent(
      "Not possible now: the exclusion was already decided.",
    );
    const [post] = sent(calls, "POST");
    expect(post?.url).toBe(`/api/v1/scope/exclusions/batches/${batchId}/approve`);
    expect(post?.init?.body).toBe(JSON.stringify({}));
  });
});
