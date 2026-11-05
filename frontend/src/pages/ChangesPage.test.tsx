import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { CHANGES_LIST, change, changeDetail, version } from "../test/changes";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const AS_OF = "2026-10-01T00:00:00Z";

function api(routes: Record<string, () => Response>): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(analyst),
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

function page(items: ReturnType<typeof change>[], extra: Record<string, unknown> = {}) {
  return json({ items, next_before_id: null, as_of: null, evaluation_run: null, ...extra });
}

describe("changes page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists changes with their review, pipeline and deployment evidence", async () => {
    api({
      [CHANGES_LIST]: () =>
        page([
          change(1),
          change(2, {
            kind: "direct_push",
            iid: null,
            title: "Hotfix",
            author: null,
            merged_by: null,
            outcome: "fail",
            reasons: ["Pushed directly to the default branch."],
            approvals: {
              status: "known",
              approved_by: [],
              required: null,
              left: null,
              self_approved: null,
              committer_approved: null,
            },
            pipeline: { status: "none" },
          }),
        ]),
    });
    renderAt("/changes");

    const table = await screen.findByRole("table", { name: "Changes, newest first" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("!41 Add rate limits");
    expect(rows[1]).toHaveTextContent("1 approval (reviewer-three); 1 approval required");
    expect(rows[1]).toHaveTextContent("Pipeline 7001 success; 120 tests, 0 failed, 2 skipped");
    expect(rows[1]).toHaveTextContent("production: success");
    expect(rows[1]).toHaveTextContent("Meets the rules");
    expect(
      within(rows[1] as HTMLElement).getByRole("link", { name: "!41 Add rate limits" }),
    ).toHaveAttribute("href", "/changes/1");
    expect(rows[2]).toHaveTextContent("Direct push aaaaaaaa Hotfix");
    expect(rows[2]).toHaveTextContent("No approvals");
    expect(rows[2]).toHaveTextContent("No pipeline");
    expect(rows[2]).toHaveTextContent("Fails a rule");
    expect(rows[2]).toHaveTextContent("Pushed directly to the default branch.");
  });

  it("shows parts that could not be read as unknown, never as passed", async () => {
    api({
      [CHANGES_LIST]: () =>
        page([
          change(3, {
            outcome: "unknown",
            approvals: { status: "unknown", reason: "approvals need GitLab Premium" },
            pipeline: { status: "unknown", reason: null },
            deployments: null,
            complete: false,
          }),
        ]),
    });
    renderAt("/changes");

    const table = await screen.findByRole("table", { name: "Changes, newest first" });
    const row = within(table).getAllByRole("row")[1] as HTMLElement;
    expect(row).toHaveTextContent("Unknown: approvals need GitLab Premium");
    expect(within(row).getAllByText("Unknown")).toHaveLength(3);
    expect(row).toHaveTextContent("Partly read; it will be read again");
    expect(row).not.toHaveTextContent("Meets the rules");
  });

  it("filters by project, kind, period and status through the URL", async () => {
    const user = userEvent.setup();
    const calls = api({
      [CHANGES_LIST]: () => page([change(1)]),
      "GET /api/v1/changes?project=105&kind=direct_push&outcome=fail&from=2026-10-01T00%3A00%3A00Z&limit=50":
        () => page([]),
    });
    const router = renderAt("/changes");

    const form = await screen.findByRole("form", { name: "Filter changes" });
    await user.type(within(form).getByLabelText("Project GitLab ID"), "105");
    await user.selectOptions(within(form).getByLabelText("Kind"), "direct_push");
    await user.selectOptions(within(form).getByLabelText("Status"), "fail");
    await user.type(within(form).getByLabelText("From (UTC)"), "2026-10-01T00:00");
    await user.click(within(form).getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("No changes match these filters.")).toBeInTheDocument();
    expect(router.state.location.search).toBe(
      "?project=105&kind=direct_push&from=2026-10-01T00%3A00%3A00Z&outcome=fail",
    );
    expect(calls.map((call) => call.url)).toContain(
      "/api/v1/changes?project=105&kind=direct_push&outcome=fail&from=2026-10-01T00%3A00%3A00Z&limit=50",
    );
  });

  it("shows a change's evidence versions with their digests", async () => {
    api({
      "GET /api/v1/changes/1": () =>
        json(
          changeDetail(1, [
            version(902, { collected_at: "2026-10-21T11:00:00Z", digest_valid: false }),
            version(901),
          ]),
        ),
    });
    renderAt("/changes/1");

    expect(
      await screen.findByRole("heading", { level: 1, name: "!41 Add rate limits" }),
    ).toBeInTheDocument();
    const summary = screen.getByRole("region", { name: "Summary" });
    expect(summary).toHaveTextContent("Merged bylead-two");
    expect(within(summary).getByRole("list", { name: "Approved by" })).toHaveTextContent(
      "reviewer-three on",
    );
    const evidence = screen.getByRole("region", { name: "Evidence" });
    const rows = within(evidence).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("(latest)");
    expect(rows[1]).toHaveTextContent("Failed: the digest does not match the stored contents");
    expect(rows[2]).toHaveTextContent("c".repeat(64));
    expect(rows[2]).toHaveTextContent("Verified");
  });

  it("says when a change does not exist", async () => {
    api({ "GET /api/v1/changes/9": () => json({ detail: "Not found" }, 404) });
    renderAt("/changes/9");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Change not found" }),
    ).toBeInTheDocument();
  });

  it("asks for the changes as of the date viewed and keeps it when opening one", async () => {
    const user = userEvent.setup();
    const run = { id: 4, finished_at: "2026-09-30T06:12:00Z" };
    const calls = api({
      [`GET /api/v1/changes?limit=50&as_of=${encodeURIComponent(AS_OF)}`]: () =>
        page([change(1)], { as_of: AS_OF, evaluation_run: run }),
      [`GET /api/v1/changes/1?as_of=${encodeURIComponent(AS_OF)}`]: () =>
        json(changeDetail(1, [version(901)], { as_of: AS_OF, evaluation_run: run })),
    });
    const router = renderAt(`/changes?as_of=${AS_OF}`);

    expect(
      await screen.findByText(
        /As of .*: from evaluation run 4, finished .*the latest finished by then/,
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "!41 Add rate limits" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "!41 Add rate limits" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/changes/1");
    expect(new URLSearchParams(router.state.location.search).get("as_of")).toBe(AS_OF);
    expect(calls.map((call) => call.url)).toContain(
      `/api/v1/changes/1?as_of=${encodeURIComponent(AS_OF)}`,
    );
  });
});
