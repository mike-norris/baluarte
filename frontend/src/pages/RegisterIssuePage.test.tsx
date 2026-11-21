import { render, screen, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { IssueDetail, IssueEvidence } from "../api/register";
import { routes } from "../routes";
import { analyst, json } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const SHA = "a".repeat(64);

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

function api(evidenceBody: IssueEvidence) {
  const routes: Record<string, () => Response> = {
    "GET /api/v1/auth/me": () => json(analyst),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/register/issues/5/1": () => json(detail),
    "GET /api/v1/register/issues/5/1/evidence": () => json(evidenceBody),
  };
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input.toString();
    const handler = routes[`${init?.method ?? "GET"} ${url}`];
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

  it("says when nothing is captured yet", async () => {
    api({ ...evidence, captured: false, status: null, gaps: [], capture: null });
    renderIssue();

    expect(await screen.findByText("Not captured yet")).toBeInTheDocument();
  });
});
