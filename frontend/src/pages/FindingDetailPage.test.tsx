import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Evaluation, FindingDetail } from "../api/evaluations";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { evaluation, findingDetail, observation } from "../test/findings";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const FINDING = "GET /api/v1/findings/5";
const HISTORY = "GET /api/v1/findings/5/evaluations?limit=50";
const EVIDENCE = "GET /api/v1/gitlab/projects/105/observations?before_id=10&limit=1";
const EXCEPTIONS =
  "GET /api/v1/exceptions?project=105&policy=corporate-default&rule=project-visibility&limit=50";

function history(items: Evaluation[], next: number | null = null) {
  return json({ items, next_before_id: next });
}

function api(detail: FindingDetail, routes: Record<string, () => Response> = {}) {
  return mockApi({
    [ME]: () => json(analyst),
    [FRESHNESS]: () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [FINDING]: () => json(detail),
    [HISTORY]: () => history([detail.latest_evaluation]),
    [EVIDENCE]: () => json({ items: [observation(9)], next_before_id: 8 }),
    [EXCEPTIONS]: () => json({ items: [], next_after_id: null }),
    ...routes,
  });
}

function renderAt(path = "/findings/5") {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

async function comparison() {
  const section = await screen.findByRole("region", { name: "Expected versus actual" });
  const value = (term: string) =>
    within(section).getByText(term, { selector: "dt" }).nextElementSibling;
  return { section, expected: value("Expected"), observed: value("Observed") };
}

describe("finding detail", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows expected versus observed for a failed rule, with its policy and project", async () => {
    api(findingDetail(5));
    renderAt();

    expect(
      await screen.findByRole("heading", { level: 1, name: "project-visibility on platform/api" }),
    ).toBeInTheDocument();
    const { section, expected, observed } = await comparison();
    expect(expected).toHaveTextContent("private");
    expect(observed).toHaveTextContent("public");
    expect(section).toHaveTextContent("Outcome: Fail. The collected value breaks the rule.");
    const summary = screen.getByRole("region", { name: "Summary" });
    expect(summary).toHaveTextContent(
      "corporate-default, version 2 (the pinned version evaluated)",
    );
    expect(summary).toHaveTextContent("project-visibility (gitlab.project.visibility)");
    expect(within(summary).getByRole("link", { name: "platform/api" })).toHaveAttribute(
      "href",
      "/inventory/projects/105",
    );
  });

  it("shows a passing, resolved finding", async () => {
    api(
      findingDetail(5, evaluation(21, { outcome: "pass", observed: "private" }), {
        state: "resolved",
        resolution: "passed",
        resolved_at: "2026-10-29T06:12:00Z",
      }),
    );
    renderAt();

    const { observed, section } = await comparison();
    expect(observed).toHaveTextContent("private");
    expect(section).toHaveTextContent("Outcome: Pass.");
    expect(screen.getByRole("region", { name: "Summary" })).toHaveTextContent(
      "Resolved on 29 Oct 2026, 06:12 UTC, because the rule passed",
    );
  });

  it("links the controls its rule type counts toward", async () => {
    api(
      findingDetail(5, evaluation(20), {
        controls: [
          { framework: "soc2", id: "CC6.1" },
          { framework: "nist_800_53_moderate", id: "AC-6(5)" },
        ],
      }),
    );
    renderAt();

    const controls = await screen.findByRole("list", { name: "Controls" });
    expect(within(controls).getByRole("link", { name: "SOC 2 CC6.1" })).toHaveAttribute(
      "href",
      "/controls/soc2/CC6.1",
    );
    expect(within(controls).getByRole("link", { name: "NIST 800-53 AC-6(5)" })).toHaveAttribute(
      "href",
      "/controls/nist_800_53_moderate/AC-6(5)",
    );
  });

  it("says when a finding counts toward no control", async () => {
    api(findingDetail(5, evaluation(20), { controls: [] }));
    renderAt();

    expect(await screen.findByRole("region", { name: "Summary" })).toHaveTextContent(
      "Not mapped to any control.",
    );
  });

  it("leaves controls out when the API does not report them", async () => {
    api(findingDetail(5));
    renderAt();

    const summary = await screen.findByRole("region", { name: "Summary" });
    expect(within(summary).queryByText("Controls")).not.toBeInTheDocument();
  });

  it("summarises a list-valued observation above its JSON and in the history", async () => {
    const tokens = evaluation(22, {
      rule_type: "gitlab.project.access_tokens",
      expected: { max_lifetime_days: 90 },
      observed: [
        {
          id: 1,
          name: "deploy",
          active: true,
          revoked: false,
          created_at: "2026-10-01T00:00:00Z",
          expires_at: null,
          last_used_at: "2026-10-30T00:00:00Z",
        },
      ],
    });
    api(findingDetail(5, tokens));
    renderAt();

    const { observed } = await comparison();
    expect(observed).toHaveTextContent("1 active token; 1 without an expiry date.");
    expect(observed?.querySelector("pre")).toHaveTextContent('"name": "deploy"');
    const historyTable = await screen.findByRole("table", {
      name: "Evaluation history, newest first",
    });
    expect(historyTable).toHaveTextContent("1 active token; 1 without an expiry date.");
    expect(historyTable).not.toHaveTextContent('"name"');
  });

  it("says a finding was resolved because its project was archived", async () => {
    api(
      findingDetail(5, evaluation(21), {
        state: "resolved",
        resolution: "project_archived",
        resolved_at: "2026-11-01T06:12:00Z",
      }),
    );
    renderAt();

    expect(await screen.findByRole("region", { name: "Summary" })).toHaveTextContent(
      "because the project was archived, and archived projects are not checked",
    );
  });

  it.each([
    ["unknown", ["collection_stale", "value_unknown"], "The projects inventory is stale."],
    ["unsupported", [], "This GitLab instance cannot report protected branches."],
  ] as const)("never shows a value for an %s result", async (outcome, reasons, reason) => {
    api(
      findingDetail(
        5,
        evaluation(22, {
          outcome,
          observed: "private",
          unknown_reasons: [...reasons],
          reason,
          observation_id: null,
        }),
      ),
    );
    renderAt();

    const { observed, section } = await comparison();
    expect(observed).toHaveTextContent(`Unknown — ${reason}`);
    expect(observed).not.toHaveTextContent("private");
    expect(section).toHaveTextContent("Not counted as passing.");
    if (reasons.length) {
      const items = within(section).getAllByRole("listitem");
      expect(items[0]).toHaveTextContent(
        "collection_stale: The collected data is older than the freshness limit. " +
          "(explains the result)",
      );
      expect(items[1]).toHaveTextContent("value_unknown: GitLab did not report the value.");
    }
    const evidence = screen.getByRole("region", { name: "Evidence" });
    expect(evidence).toHaveTextContent("No evidence record is linked to this result");
    expect(evidence).toHaveTextContent("is not a compliance claim");
  });

  it("shows the judged evidence with its digest", async () => {
    api(findingDetail(5));
    renderAt();

    const evidence = await screen.findByRole("region", { name: "Evidence" });
    expect(await within(evidence).findByText("a".repeat(64))).toBeInTheDocument();
    expect(evidence).toHaveTextContent("Verified: the digest matches the stored contents.");
    expect(evidence).toHaveTextContent("/projects/105");
    expect(within(evidence).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("warns when the evidence digest no longer matches", async () => {
    api(findingDetail(5), {
      [EVIDENCE]: () =>
        json({ items: [observation(9, { digest_valid: false })], next_before_id: 8 }),
    });
    renderAt();

    const evidence = await screen.findByRole("region", { name: "Evidence" });
    expect(await within(evidence).findByRole("alert")).toHaveTextContent(
      "failed its integrity check",
    );
    expect(evidence).toHaveTextContent("Failed: the digest does not match the stored contents.");
  });

  it("explains a result made unknown by an altered evidence record", async () => {
    api(
      findingDetail(
        5,
        evaluation(23, {
          outcome: "unknown",
          unknown_reasons: ["evidence_digest_mismatch"],
          reason: "The evidence no longer matches its digest.",
        }),
      ),
      {
        [EVIDENCE]: () =>
          json({ items: [observation(9, { digest_valid: false })], next_before_id: 8 }),
      },
    );
    renderAt();

    const { section } = await comparison();
    expect(section).toHaveTextContent(
      "evidence_digest_mismatch: The evidence record's SHA-256 digest no longer matches",
    );
    const evidence = screen.getByRole("region", { name: "Evidence" });
    expect(await within(evidence).findByRole("alert")).toHaveTextContent(
      "The result was recorded as unknown.",
    );
  });

  it("warns when the linked evidence cannot be found", async () => {
    api(findingDetail(5), {
      [EVIDENCE]: () => json({ items: [observation(8)], next_before_id: 7 }),
    });
    renderAt();

    const evidence = await screen.findByRole("region", { name: "Evidence" });
    expect(await within(evidence).findByRole("alert")).toHaveTextContent(
      "The linked evidence record (ID 9) could not be found for this project.",
    );
  });

  it("lists the history newest first and loads older evaluations", async () => {
    const user = userEvent.setup();
    const latest = evaluation(30, { evaluated_at: "2026-10-29T06:12:00Z" });
    api(findingDetail(5, latest), {
      [HISTORY]: () =>
        history(
          [
            latest,
            evaluation(29, {
              outcome: "unknown",
              observed: null,
              evaluated_at: "2026-10-28T18:12:00Z",
              reason: "The projects inventory is stale.",
            }),
          ],
          29,
        ),
      "GET /api/v1/findings/5/evaluations?before_id=29&limit=50": () =>
        history([
          evaluation(28, {
            outcome: "pass",
            observed: "private",
            policy: { policy_id: 1, name: "corporate-default", version: 1 },
            evaluated_at: "2026-10-28T06:12:00Z",
          }),
        ]),
    });
    renderAt();

    const table = await screen.findByRole("table", { name: "Evaluation history, newest first" });
    const outcomesShown = () =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => row.querySelectorAll("td")[0]?.textContent);
    expect(outcomesShown()).toEqual(["Fail", "Unknown"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("(latest)");
    expect(rows[1]).toHaveTextContent("The projects inventory is stale.");

    const older = screen.getByRole("button", { name: "Load older evaluations" });
    await user.click(older);
    await within(table).findByText("v1");
    expect(outcomesShown()).toEqual(["Fail", "Unknown", "Pass"]);
    expect(older).not.toBeInTheDocument();
  });

  it("says when a finding does not exist", async () => {
    mockApi({
      [ME]: () => json(analyst),
      [FRESHNESS]: () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
      "GET /api/v1/findings/77": () => json({ detail: "No such finding" }, 404),
    });
    renderAt("/findings/77");

    expect(await screen.findByRole("heading", { name: "Finding not found" })).toBeInTheDocument();
    expect(screen.getByText("There is no finding with ID 77.")).toBeInTheDocument();
  });

  it("does not request an invalid finding ID", async () => {
    const calls = mockApi({
      [ME]: () => json(analyst),
      [FRESHNESS]: () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    });
    renderAt("/findings/abc");

    expect(await screen.findByRole("heading", { name: "Finding not found" })).toBeInTheDocument();
    expect(calls.some((call) => call.url.startsWith("/api/v1/findings"))).toBe(false);
  });

  it("offers a retry when the finding cannot be loaded", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const detail = findingDetail(5);
    api(detail, {
      [FINDING]: () => {
        attempts += 1;
        return attempts === 1 ? json({ detail: "boom" }, 500) : json(detail);
      },
    });
    renderAt();

    expect(await screen.findByText("The API returned an error (500).")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "project-visibility on platform/api" }),
    ).toBeInTheDocument();
  });

  it("returns to sign-in when the session has ended", async () => {
    let signedIn = true;
    mockApi({
      [ME]: () => (signedIn ? json(analyst) : json({ detail: "no session" }, 401)),
      [FRESHNESS]: () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
      [FINDING]: () => {
        signedIn = false;
        return json({ detail: "no session" }, 401);
      },
    });
    renderAt();

    expect(await screen.findByRole("heading", { name: "Sign in to Baluarte" })).toBeInTheDocument();
  });
  it("lists the changes a change rule failed on, each linked to its evidence", async () => {
    const latest = evaluation(20, {
      rule_id: "changes-reviewed",
      rule_type: "gitlab.change.reviewed",
      expected: { min_approvals: 1, forbid_self_approval: true, allow_direct_push: false },
      observed: {
        window_days: 90,
        changes: 12,
        passed: 9,
        failed: 3,
        unknown: 0,
        failing: [
          { id: 31, kind: "merge_request", iid: 44, sha: "a".repeat(40), title: "Bump deps" },
          { id: 32, kind: "direct_push", iid: null, sha: "b".repeat(40), title: "Hotfix" },
        ],
        failing_total: 3,
        gaps: [],
      },
    });
    api(findingDetail(5, latest), {
      [FINDING]: () => json(findingDetail(5, latest)),
      [HISTORY]: () => history([latest]),
      "GET /api/v1/exceptions?project=105&policy=corporate-default&rule=changes-reviewed&limit=50":
        () => json({ items: [], next_after_id: null }),
    });
    renderAt();

    const { observed } = await comparison();
    expect(observed).toHaveTextContent("12 changes in the window: 9 passed, 3 failed, 0 unknown");
    const failing = screen.getByRole("region", { name: "Failing changes" });
    expect(failing).toHaveTextContent("The first 2 of 3 failing changes.");
    expect(within(failing).getByRole("link", { name: "!44 Bump deps" })).toHaveAttribute(
      "href",
      "/changes/31",
    );
    expect(
      within(failing).getByRole("link", { name: "Direct push bbbbbbbb Hotfix" }),
    ).toHaveAttribute("href", "/changes/32");
    expect(
      within(failing).getByRole("link", { name: "Failing changes for this project" }),
    ).toHaveAttribute("href", "/changes?project=105&outcome=fail");
  });

  it("does not list failing changes for other rule types", async () => {
    api(findingDetail(5));
    renderAt();

    await comparison();
    expect(screen.queryByRole("region", { name: "Failing changes" })).not.toBeInTheDocument();
  });
});
