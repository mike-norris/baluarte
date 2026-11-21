import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { IssueDetail, IssueEvidence, IssueMapping, IssueMappings } from "../api/register";
import { routes } from "../routes";
import { analyst, json } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const SHA = "a".repeat(64);
const writer = { ...analyst, permissions: [...analyst.permissions, "register:write"] };

const detail: IssueDetail = {
  project_id: 5,
  project: "acme/api",
  iid: 1,
  auditable: true,
  decisions: [],
  issue: {
    project_id: 5,
    project: "acme/api",
    connection_id: 1,
    iid: 1,
    issue_type: "issue",
    title: "Quarterly access review",
    state: "closed",
    labels: ["security"],
    author: "ada",
    assignees: [],
    milestone: "Q4",
    web_url: "https://gitlab.example.test/acme/api/-/issues/1",
    confidential: false,
    opened_at: null,
    updated_at: null,
    closed_at: "2026-11-03T00:00:00Z",
    read_at: "2026-11-21T00:00:00Z",
    category: "security_finding",
    excluded: false,
    why: [
      {
        kind: "rule",
        rule_id: 1,
        rule_name: "Security issues",
        decision_id: null,
        reason: null,
        decided_by: null,
        decided_at: null,
      },
    ],
  },
};

const evidence: IssueEvidence = {
  project_id: 5,
  iid: 1,
  captured: true,
  status: "partial",
  gaps: ["notes: the token may not read /projects/42/issues/1/notes (403)"],
  captured_at: "2026-11-21T10:00:00Z",
  captured_for: "2026-11-03T00:00:00Z",
  observation_id: 77,
  payload_digest: "b".repeat(64),
  digest_verified: true,
  capture: {
    issue: {
      title: "Quarterly access review",
      description: "Reviewed every maintainer.",
      state: "closed",
      closed_by: "grace",
      web_url: null,
    },
    notes: [],
    history: [
      {
        kind: "state",
        id: 1,
        user: "grace",
        created_at: "2026-11-03T00:00:00Z",
        action: null,
        state: "closed",
        label: null,
        milestone: null,
        merge_request: null,
      },
    ],
    links: [],
    merge_requests: [
      {
        project_id: 42,
        iid: 5,
        relation: "closes",
        title: "Fix access list",
        state: "merged",
        web_url: "https://gitlab.example.test/acme/api/-/merge_requests/5",
        author: "ada",
        reviewers: [],
        target_branch: "main",
        merged_at: "2026-11-02T12:00:00Z",
        merged_by: "grace",
        merge_commit_sha: "abc",
        approvals: { approved: true, approved_by: ["linus"], approvals_required: 1 },
        pipelines: [{ id: 9, status: "success", ref: "fix", web_url: null }],
      },
    ],
    releases: [],
    attachments: [
      {
        secret: "s",
        filename: "review.png",
        referenced_in: ["description"],
        status: "ok",
        sha256: SHA,
        size_bytes: 2048,
        content_type: "image/png",
        error: null,
      },
      {
        secret: "t",
        filename: "big.zip",
        referenced_in: ["note:11"],
        status: "too_large",
        sha256: null,
        size_bytes: null,
        content_type: null,
        error: "too large",
      },
    ],
    sources: { issue: "ok", notes: "forbidden", releases: "ok" },
  },
};

const proposed: IssueMapping = {
  id: 3,
  project_id: 5,
  project: "acme/api",
  iid: 1,
  issue_title: "Quarterly access review",
  framework: "soc2",
  control_id: "CC8.1",
  control_title: "Changes are authorized, tested and approved before release",
  rationale: "The closing merge request was approved by a second person.",
  state: "proposed",
  observation_id: 77,
  supersedes_id: null,
  mapping_version: "2026.11.1",
  review_required: true,
  self_approved: false,
  development_decision: false,
  proposed_by: { actor: "user:2", user_id: 2, display_name: "Grace" },
  proposed_at: "2026-11-21T10:00:00Z",
  decided_by: null,
  decided_at: null,
  decision_note: null,
  withdrawn_by: null,
  withdrawn_at: null,
  withdraw_reason: null,
  evidence: [
    {
      key: "merge_request:42:5",
      kind: "merge_request",
      label: "Merge request !5 (merged, approved by linus)",
      occurred_at: "2026-11-02T12:00:00Z",
      rationale: "A second person approved it.",
      timing: null,
    },
  ],
  gaps: [
    { code: "awaiting_review", message: "The mapping is waiting for a second person's review." },
  ],
  can_decide: true,
};

const mappings: IssueMappings = {
  project_id: 5,
  iid: 1,
  review_required: true,
  observation_id: 77,
  citable: [
    { key: "note:11", kind: "note", label: "Comment by ada: Reviewed", occurred_at: null },
    { key: "event:state:1", kind: "event", label: "grace closed the issue", occurred_at: null },
  ],
  items: [proposed],
};

const sent: { method: string; url: string; body: unknown }[] = [];

function api(evidenceBody: IssueEvidence) {
  sent.length = 0;
  const routes: Record<string, () => Response> = {
    "GET /api/v1/register/issues/5/1/mappings": () => json(mappings),
    "GET /api/v1/register/periods": () => json({ items: [] }),
    "GET /api/v1/controls?framework=soc2": () =>
      json({
        as_of: null,
        mapping_version: "2026.11.1",
        frameworks: [],
        controls: [{ framework: "soc2", id: "CC6.1", title: "Logical access", area: "Access" }],
      }),
    "POST /api/v1/register/mappings": () => json({ ...proposed, id: 4 }, 201),
    "POST /api/v1/register/mappings/3/approve": () => json({ ...proposed, state: "active" }),
    "GET /api/v1/auth/me": () => json(writer),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/register/issues/5/1": () => json(detail),
    "GET /api/v1/register/issues/5/1/evidence": () => json(evidenceBody),
  };
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input.toString();
    const method = init?.method ?? "GET";
    if (method !== "GET") {
      sent.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
    }
    const handler = routes[`${method} ${url}`];
    return handler ? Promise.resolve(handler()) : Promise.reject(new TypeError(url));
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderIssue() {
  render(
    <RouterProvider
      router={createMemoryRouter(routes, { initialEntries: ["/register/issues/5/1"] })}
    />,
  );
}

describe("RegisterIssuePage", () => {
  it("shows the capture, its gaps, merge requests and attachments", async () => {
    api(evidence);
    renderIssue();

    expect(await screen.findByText("Reviewed every maintainer.")).toBeInTheDocument();
    expect(screen.getByText("Auditable")).toBeInTheDocument();
    expect(screen.getByText("Rule: Security issues")).toBeInTheDocument();
    expect(screen.getByText("Partial")).toBeInTheDocument();
    expect(screen.getByText(/the token may not read/)).toBeInTheDocument();
    const mrs = within(screen.getByRole("region", { name: "Merge requests" }));
    expect(mrs.getByText("Closes the issue")).toBeInTheDocument();
    expect(mrs.getByText("linus")).toBeInTheDocument();
    expect(mrs.getByText("success")).toBeInTheDocument();
    const files = within(screen.getByRole("region", { name: "Attachments" }));
    expect(files.getByRole("link", { name: "review.png" })).toHaveAttribute(
      "href",
      `/api/v1/register/files/${SHA}`,
    );
    expect(files.queryByRole("link", { name: "big.zip" })).not.toBeInTheDocument();
    expect(files.getByText("too large")).toBeInTheDocument();
    expect(screen.getByText("closed the issue")).toBeInTheDocument();
  });

  it("shows mappings with their gaps and lets a reviewer approve", async () => {
    api(evidence);
    renderIssue();

    const card = within(await screen.findByRole("article", { name: "SOC 2 CC8.1" }));
    expect(card.getByText("Awaiting review", { selector: ".badge" })).toBeInTheDocument();
    expect(card.getByText(/waiting for a second person/)).toBeInTheDocument();
    await userEvent.click(card.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(sent).toContainEqual({
        method: "POST",
        url: "/api/v1/register/mappings/3/approve",
        body: { note: null },
      });
    });
  });

  it("proposes a mapping citing items, each with why", async () => {
    api(evidence);
    renderIssue();

    const form = within(await screen.findByRole("form", { name: "Map to a control" }));
    await userEvent.selectOptions(await form.findByRole("combobox", { name: "Control" }), "CC6.1");
    await userEvent.type(
      form.getByRole("textbox", { name: "Rationale" }),
      "The review comment records who checked the access list.",
    );
    await userEvent.click(form.getByRole("checkbox", { name: /Comment by ada/ }));
    await userEvent.type(
      form.getByRole("textbox", { name: "Why this item" }),
      "Names the reviewer.",
    );
    await userEvent.click(form.getByRole("button", { name: "Propose mapping" }));

    await waitFor(() => {
      expect(sent).toContainEqual({
        method: "POST",
        url: "/api/v1/register/mappings",
        body: {
          project_id: 5,
          iid: 1,
          framework: "soc2",
          control_id: "CC6.1",
          rationale: "The review comment records who checked the access list.",
          items: [{ key: "note:11", rationale: "Names the reviewer." }],
        },
      });
    });
  });

  it("says when nothing is captured yet", async () => {
    api({ ...evidence, captured: false, status: null, gaps: [], capture: null });
    renderIssue();

    expect(await screen.findByText("Not captured yet")).toBeInTheDocument();
  });
});
