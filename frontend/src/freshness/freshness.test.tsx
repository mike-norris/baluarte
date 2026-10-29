import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { FreshnessReport } from "../api/gitlab";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness, kind } from "../test/freshness";
import { collectionSignal, evaluationSignal, freshnessState } from "./context";
import { REFRESH_INTERVAL_MS } from "./FreshnessProvider";
import { worst } from "./status";

const ME = "GET /api/v1/auth/me";
const FRESHNESS = "GET /api/v1/gitlab/freshness";
const FRESHNESS_URL = "/api/v1/gitlab/freshness";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const view = render(<RouterProvider router={router} />);
  return { router, view };
}

function api(
  report: () => Response | Promise<Response>,
  evaluation: () => Response | Promise<Response> = () => json(evaluationFreshness()),
) {
  return mockApi({
    [ME]: () => json(analyst),
    [FRESHNESS]: report,
    [EVALUATION_FRESHNESS]: evaluation,
  });
}

function indicator(name: RegExp | string = /^GitLab data: /) {
  return screen.findByRole("link", { name });
}

function freshnessCalls(calls: { url: string }[]) {
  return calls.filter((call) => call.url === FRESHNESS_URL).length;
}

function liveRegion(): HTMLElement {
  const region = document.querySelector<HTMLElement>("header [aria-live='polite']");
  if (!region) throw new Error("no live region in the header");
  return region;
}

describe("header freshness indicator", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it.each([
    ["failed", "Failed"],
    ["never", "Never collected"],
    ["stale", "Stale"],
    ["partial", "Partial"],
    ["fresh", "Up to date"],
    ["not_configured", "Not configured"],
  ] as const)("shows %s as text and links to the inventory", async (status, label) => {
    api(() => json(freshness({ status })));
    renderAt("/policies");

    const link = await indicator(`GitLab data: ${label}`);
    expect(link).toHaveAttribute("href", "/inventory");
  });

  it("announces failed and stale through a polite live region, but not healthy states", async () => {
    let status: FreshnessReport["status"] = "fresh";
    api(() => json(freshness({ status })));
    const user = userEvent.setup();
    renderAt("/policies");

    await indicator("GitLab data: Up to date");
    expect(liveRegion()).toBeEmptyDOMElement();

    status = "stale";
    await user.click(screen.getByRole("link", { name: "Settings" }));
    await indicator("GitLab data: Stale");
    expect(liveRegion()).toHaveTextContent("GitLab data is stale.");
    const region = liveRegion();

    // Another navigation re-checks; the same status leaves the region unchanged, so it is
    // not announced again.
    await user.click(screen.getByRole("link", { name: "Policies" }));
    await indicator("GitLab data: Stale");
    expect(liveRegion()).toBe(region);
    expect(region).toHaveTextContent("GitLab data is stale.");
  });

  it("shows status unknown when the check fails", async () => {
    api(() => json({ detail: "boom" }, 500));
    renderAt("/policies");

    await indicator("GitLab data: Status unknown");
    expect(liveRegion()).toHaveTextContent("GitLab collection status could not be checked.");
  });

  it("replaces an earlier result with status unknown when a re-check fails", async () => {
    let fail = false;
    api(() => (fail ? Promise.reject(new TypeError("offline")) : json(freshness())));
    const user = userEvent.setup();
    renderAt("/policies");

    await indicator("GitLab data: Up to date");
    fail = true;
    await user.click(screen.getByRole("link", { name: "Settings" }));

    await indicator("GitLab data: Status unknown");
  });

  it("is not shown, and does not check, when signed out", async () => {
    const calls = mockApi({ [ME]: () => json({}, 401) });
    renderAt("/sign-in");

    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^GitLab data: / })).not.toBeInTheDocument();
    expect(freshnessCalls(calls)).toBe(0);
  });

  it("re-checks on navigation", async () => {
    const calls = api(() => json(freshness()));
    const user = userEvent.setup();
    renderAt("/policies");
    await indicator();
    const before = freshnessCalls(calls);

    await user.click(screen.getByRole("link", { name: "Settings" }));

    await vi.waitFor(() => {
      expect(freshnessCalls(calls)).toBe(before + 1);
    });
  });

  it("re-checks on an interval while the page stays open", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let status: FreshnessReport["status"] = "fresh";
    const calls = api(() => json(freshness({ status })));
    renderAt("/policies");
    await indicator("GitLab data: Up to date");
    const before = freshnessCalls(calls);

    status = "failed";
    act(() => {
      vi.advanceTimersByTime(REFRESH_INTERVAL_MS);
    });

    await indicator("GitLab data: Failed");
    expect(freshnessCalls(calls)).toBe(before + 1);
  });

  it("aborts an in-flight check when unmounted", async () => {
    const calls = api(() => new Promise<Response>(() => undefined));
    const { view } = renderAt("/policies");
    await indicator("GitLab data: Checking…");

    view.unmount();

    const pending = calls.filter((call) => call.url === FRESHNESS_URL);
    expect(pending.length).toBeGreaterThan(0);
    for (const call of pending) expect(call.init?.signal?.aborted).toBe(true);
  });
});

describe("collection context on GitLab-derived pages", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each(["/evidence"])(
    "shows collection and evaluation status on %s",
    async (path) => {
      api(() => json(freshness({ status: "stale", collections: { projects: kind("stale") } })));
      renderAt(path);
      await indicator("GitLab data: Stale");

      const note = screen.getByRole("complementary", { name: "Data freshness" });
      expect(note).toHaveTextContent("GitLab collection status: Stale");
      expect(note).toHaveTextContent("Oldest successful collection: 29 Oct 2026, 06:00 UTC");
      expect(note).toHaveTextContent("Policy evaluation status: Up to date");
      expect(note).toHaveTextContent("Last successful evaluation: 29 Oct 2026, 06:12 UTC");
      expect(note).not.toHaveTextContent("collected after the last successful evaluation");
    },
  );

  it("says when results predate the latest collection", async () => {
    api(
      () => json(freshness()),
      () => json(evaluationFreshness({ behind_collection: true })),
    );
    renderAt("/evidence");
    await indicator("GitLab data: Up to date");

    const note = screen.getByRole("complementary", { name: "Data freshness" });
    await vi.waitFor(() => {
      expect(note).toHaveTextContent("collected after the last successful evaluation");
    });
  });

  it("does not claim a collection time when the status is unknown", async () => {
    api(() => json({}, 503));
    renderAt("/evidence");
    await indicator("GitLab data: Status unknown");

    const note = screen.getByRole("complementary", { name: "Data freshness" });
    expect(note).toHaveTextContent("GitLab collection status: Status unknown");
    expect(note).not.toHaveTextContent("Oldest successful collection");
  });

  it("is not shown on pages that do not use GitLab data", async () => {
    api(() => json(freshness()));
    renderAt("/policies");

    await indicator();
    expect(screen.queryByRole("complementary", { name: "Data freshness" })).not.toBeInTheDocument();
  });
});

describe("freshness signals", () => {
  it("orders statuses worst first, with unknown never better than stale", () => {
    expect(worst(["fresh", "partial"])).toBe("partial");
    expect(worst(["stale", "unknown"])).toBe("unknown");
    expect(worst(["unknown", "failed"])).toBe("failed");
    expect(worst([])).toBe("fresh");
  });

  it("uses the oldest success across kinds, and none if any kind never succeeded", () => {
    const report = freshness({
      collections: {
        groups: kind("fresh", { last_success_at: "2026-10-29T05:00:00Z" }),
        projects: kind("fresh", { last_success_at: "2026-10-29T06:30:00Z" }),
      },
    });
    expect(collectionSignal(report, undefined).lastSuccessAt).toBe("2026-10-29T05:00:00Z");

    const partial = freshness({ collections: { groups: kind("fresh"), projects: kind("never") } });
    expect(collectionSignal(partial, undefined).lastSuccessAt).toBeNull();
  });

  it("reports checking before the first result", () => {
    expect(freshnessState({}, {}).overall).toBe("checking");
  });

  it("takes the worst of collection and evaluation, naming the signal that sets it", () => {
    const failedEvaluation = freshnessState(
      { data: freshness() },
      { data: evaluationFreshness({ status: "failed" }) },
    );
    expect(failedEvaluation.overall).toBe("failed");
    expect(failedEvaluation.worstSignal.source).toBe("evaluation");

    const both = freshnessState(
      { data: freshness({ status: "stale" }) },
      { data: evaluationFreshness({ status: "stale" }) },
    );
    expect(both.worstSignal.source).toBe("collection");
  });

  it("treats an evaluation status that could not be read as unknown, with no success time", () => {
    const signal = evaluationSignal(evaluationFreshness(), new ApiError(500, "boom"));
    expect(signal.status).toBe("unknown");
    expect(signal.lastSuccessAt).toBeNull();
  });
});

describe("header with evaluation freshness", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a failed evaluation even when collection is fresh, linking to the dashboard", async () => {
    api(
      () => json(freshness()),
      () => json(evaluationFreshness({ status: "failed" })),
    );
    renderAt("/policies");

    const link = await indicator("GitLab data: Failed");
    expect(link).toHaveAttribute("href", "/");
    expect(liveRegion()).toHaveTextContent(
      "The latest policy evaluation failed. Compliance results may be out of date.",
    );
  });

  it("names an evaluation that never ran", async () => {
    api(
      () => json(freshness()),
      () =>
        json(
          evaluationFreshness({
            status: "never",
            last_run: null,
            last_success_at: null,
          }),
        ),
    );
    renderAt("/policies");

    await indicator("GitLab data: Never evaluated");
  });

  it("shows status unknown when the evaluation check fails", async () => {
    api(
      () => json(freshness()),
      () => json({ detail: "boom" }, 500),
    );
    renderAt("/policies");

    await indicator("GitLab data: Status unknown");
    expect(liveRegion()).toHaveTextContent("Policy evaluation status could not be checked.");
  });

  it("keeps linking to the inventory when collection is as bad as evaluation", async () => {
    api(
      () => json(freshness({ status: "failed" })),
      () => json(evaluationFreshness({ status: "failed" })),
    );
    renderAt("/policies");

    expect(await indicator("GitLab data: Failed")).toHaveAttribute("href", "/inventory");
  });
});
