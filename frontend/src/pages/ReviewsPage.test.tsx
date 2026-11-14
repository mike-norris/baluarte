import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json } from "../test/api";
import { evaluationFreshness, freshness } from "../test/freshness";

const reviewer: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "remediation:approve"],
};

const person = (user_id: number, display_name: string) => ({
  actor: `user:${String(user_id)}`,
  user_id,
  display_name,
});

function plan(id: number, by: ReturnType<typeof person>) {
  return {
    id,
    state: "submitted",
    scope: { groups: ["acme"] },
    taxonomy: null,
    totals: {},
    steps: 12,
    manual: 0,
    plan_digest: "a".repeat(64),
    created_by: by,
    created_at: "2026-11-13T10:00:00Z",
    expires_at: "2099-11-14T10:00:00Z",
    decided_by: null,
    decided_at: null,
    finished_at: null,
  };
}

/** Answers each list by its path, whatever the query; anything unexpected fails. */
function serve(user: CurrentUser, lists: Record<string, unknown>, failing: string[] = []) {
  vi.stubGlobal("fetch", (input: RequestInfo | URL) => {
    const url = new URL(input.toString(), "http://localhost");
    const path = url.pathname;
    if (path === "/api/v1/auth/me") return Promise.resolve(json(user));
    if (path === "/api/v1/gitlab/freshness") return Promise.resolve(json(freshness()));
    if (path === "/api/v1/evaluations/freshness") {
      return Promise.resolve(json(evaluationFreshness()));
    }
    if (failing.includes(path)) return Promise.resolve(json({ detail: "boom" }, 500));
    if (path in lists) return Promise.resolve(json(lists[path]));
    return Promise.reject(new TypeError(`unexpected fetch: ${path}`));
  });
}

const empty = {
  "/api/v1/policy-changes": { review_required: true, items: [], next_before_id: null },
  "/api/v1/labels/taxonomy": { current: null, versions: [], review_required: true },
  "/api/v1/labels/plans": { items: [], next_after_id: null },
  "/api/v1/issue-plans": { items: [], next_after_id: null },
  "/api/v1/remediation/plans": { items: [], next_after_id: null },
  "/api/v1/exceptions": { items: [], next_after_id: null },
  "/api/v1/scope/exclusions": { items: [] },
  "/api/v1/control-mapping/changes": { items: [] },
};

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("review queue", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists what the user may decide, and what they proposed under its own tab", async () => {
    serve(reviewer, {
      ...empty,
      "/api/v1/labels/plans": {
        items: [plan(4, person(2, "Grace Hopper"))],
        next_after_id: null,
      },
      "/api/v1/issue-plans": { items: [plan(9, person(1, "Ada Lovelace"))], next_after_id: null },
    });
    const user = userEvent.setup();
    renderAt("/reviews");

    const waiting = await screen.findByRole("table", { name: "Waiting for you" });
    const rows = within(waiting).getAllByRole("row");
    expect(rows).toHaveLength(2);
    expect(within(rows[1] as HTMLElement).getByRole("link")).toHaveAttribute(
      "href",
      "/labels/plans/4",
    );
    expect(rows[1]).toHaveTextContent("Grace Hopper");

    await user.click(screen.getByRole("link", { name: "Proposed by you" }));
    const mine = await screen.findByRole("table", { name: "Proposed by you" });
    expect(within(mine).getByRole("link", { name: "Issue plan 9: 12 steps" })).toHaveAttribute(
      "href",
      "/time/issue-plans/9",
    );
  });

  it("leaves the user's own items out of their queue outside development mode", async () => {
    serve(reviewer, {
      ...empty,
      "/api/v1/labels/plans": { items: [plan(4, person(1, "Ada Lovelace"))], next_after_id: null },
    });
    renderAt("/reviews");
    expect(await screen.findByText("Nothing is waiting for your decision.")).toBeInTheDocument();
  });

  it("includes the user's own items in development mode", async () => {
    serve(
      { ...reviewer, development_mode: true },
      {
        ...empty,
        "/api/v1/labels/plans": {
          items: [plan(4, person(1, "Ada Lovelace"))],
          next_after_id: null,
        },
      },
    );
    renderAt("/reviews");
    expect(await screen.findByRole("link", { name: "Label plan 4: 12 steps" })).toBeInTheDocument();
  });

  it("says which lists could not be read rather than hiding the gap", async () => {
    serve(reviewer, empty, ["/api/v1/exceptions"]);
    renderAt("/reviews");
    expect(
      await screen.findByText(/Items of these kinds may be missing: exception\./),
    ).toBeInTheDocument();
  });
});
