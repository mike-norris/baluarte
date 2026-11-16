import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

type Item = {
  kind: string;
  id: number;
  title: string;
  proposed_by: { user_id: number | null; display_name: string | null };
  proposed_at: string;
  expires_at: string | null;
  can_decide: boolean;
  framework?: string | null;
  control_id?: string | null;
};

function item(
  kind: string,
  id: number,
  title: string,
  by: number,
  extra: Partial<Item> = {},
): Item {
  return {
    kind,
    id,
    title,
    proposed_by: { user_id: by, display_name: by === 1 ? "Ada Lovelace" : "Grace Hopper" },
    proposed_at: "2026-11-13T10:00:00Z",
    expires_at: "2099-11-14T10:00:00Z",
    can_decide: by !== 1,
    ...extra,
  };
}

function serve(queue: unknown, user: CurrentUser = analyst): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/reviews": () => json(queue),
  });
}

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
    const calls = serve({
      items: [
        item("label_plan", 4, "Label plan 4: 12 steps", 2),
        item("issue_plan", 9, "Issue plan 9: 12 steps", 1),
        item("mapping_change", 3, "SOC2 CC8.1", 2, {
          expires_at: null,
          framework: "soc2",
          control_id: "CC8.1",
        }),
      ],
      partial: [],
    });
    const user = userEvent.setup();
    renderAt("/reviews");

    const waiting = await screen.findByRole("table", { name: "Waiting for you" });
    const rows = within(waiting).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1] as HTMLElement).getByRole("link")).toHaveAttribute(
      "href",
      "/labels/plans/4",
    );
    expect(rows[1]).toHaveTextContent("Grace Hopper");
    expect(within(waiting).getByRole("link", { name: "SOC2 CC8.1" })).toHaveAttribute(
      "href",
      "/controls/soc2/CC8.1",
    );

    await user.click(screen.getByRole("link", { name: "Proposed by you" }));
    const mine = await screen.findByRole("table", { name: "Proposed by you" });
    expect(within(mine).getByRole("link", { name: "Issue plan 9: 12 steps" })).toHaveAttribute(
      "href",
      "/time/issue-plans/9",
    );
    // One request for the whole queue.
    expect(calls.filter((call) => call.url.startsWith("/api/v1/reviews"))).toHaveLength(1);
  });

  it("leaves out items the API says the user may not decide", async () => {
    serve({ items: [item("label_plan", 4, "Label plan 4: 12 steps", 1)], partial: [] });
    renderAt("/reviews");
    expect(await screen.findByText("Nothing is waiting for your decision.")).toBeInTheDocument();
  });

  it("says which kinds have more waiting than it shows", async () => {
    serve({ items: [], partial: ["exception"] });
    renderAt("/reviews");
    expect(
      await screen.findByText(/More are waiting than shown here for: exception\./),
    ).toBeInTheDocument();
  });
});
