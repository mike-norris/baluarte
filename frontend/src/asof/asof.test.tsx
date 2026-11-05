import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { control, controlDetail, controlList, PENDING_MAPPING } from "../test/controls";
import { evaluation, finding, findingDetail, observation } from "../test/findings";
import {
  EVALUATION_FRESHNESS,
  evaluationFreshness,
  evaluationRun,
  freshness,
} from "../test/freshness";
import { asOfAware, asOfProblem, parseAsOf, withAsOf } from "./format";

const AS_OF = "2026-10-01T00:00:00Z";
const Q = encodeURIComponent(AS_OF);
const RUN = { id: 4, finished_at: "2026-09-30T06:12:00Z" };
const EXCEPTIONS =
  "GET /api/v1/exceptions?project=105&policy=corporate-default&rule=project-visibility&limit=50";

const planner: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "exception:request", "remediation:plan"],
};

function api(routes: Record<string, () => Response>, user: CurrentUser = analyst): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
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

function asOfIn(router: ReturnType<typeof renderAt>): string | null {
  return new URLSearchParams(router.state.location.search).get("as_of");
}

function findings(extra: Record<string, unknown> = {}) {
  return json({ items: [finding(5)], next_after_id: null, ...extra });
}

const past = { as_of: AS_OF, evaluation_run: RUN };

describe("as-of values", () => {
  it("reads only UTC instants from the URL", () => {
    expect(parseAsOf("2026-10-01T00:00:00Z")).toBe(AS_OF);
    expect(parseAsOf("2026-10-01T00:00Z")).toBe(AS_OF);
    expect(parseAsOf("2026-10-01T00:00:00+02:00")).toBeNull();
    expect(parseAsOf("yesterday")).toBeNull();
    expect(parseAsOf(null)).toBeNull();
  });

  it("knows which pages can show the past", () => {
    for (const path of ["/", "/findings", "/findings/5", "/controls", "/controls/soc2/CC8.1"]) {
      expect(asOfAware(path)).toBe(true);
    }
    expect(asOfAware("/inventory/projects/105")).toBe(true);
    expect(asOfAware("/changes/3")).toBe(true);
    for (const path of ["/inventory", "/inventory/groups/42", "/governance", "/evidence"]) {
      expect(asOfAware(path)).toBe(false);
    }
  });

  it("refuses an empty or future date", () => {
    const now = new Date("2026-11-05T12:00:00Z");
    expect(asOfProblem("", now)).toBe("Enter a date and time.");
    expect(asOfProblem("2026-11-06T00:00", now)).toBe("Choose a date and time in the past.");
    expect(asOfProblem("2026-10-01T00:00", now)).toBeNull();
  });

  it("sets and removes the parameter on a link", () => {
    expect(withAsOf("/findings?state=open", AS_OF)).toBe(`/findings?state=open&as_of=${Q}`);
    expect(withAsOf(`/findings?as_of=${Q}`, null)).toBe("/findings");
  });
});

describe("past-date view", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("chooses a date in the header, then goes back to now", async () => {
    const user = userEvent.setup();
    const calls = api({
      "GET /api/v1/findings?limit=50": () => findings(),
      [`GET /api/v1/findings?limit=50&as_of=${Q}`]: () => findings(past),
    });
    const router = renderAt("/findings");

    await user.click(await screen.findByRole("button", { name: "Viewing now" }));
    const form = screen.getByRole("form", { name: "View as of a past date" });
    await user.type(within(form).getByLabelText("View as of (UTC)"), "2026-10-01T00:00");
    await user.click(within(form).getByRole("button", { name: "View" }));

    expect(asOfIn(router)).toBe(AS_OF);
    const banner = await screen.findByRole("region", { name: "Past-date view" });
    expect(banner).toHaveTextContent("Viewing as of 1 Oct 2026, 00:00 UTC.");
    expect(banner).toHaveTextContent("Nothing can be changed in this view.");
    expect(await screen.findByText(/from evaluation run 4/)).toBeInTheDocument();
    expect(calls.map((call) => call.url)).toContain(`/api/v1/findings?limit=50&as_of=${Q}`);

    await user.click(within(banner).getByRole("button", { name: "Back to now" }));
    expect(asOfIn(router)).toBeNull();
    expect(screen.queryByRole("region", { name: "Past-date view" })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Viewing now" })).toBeInTheDocument();
  });

  it("refuses a future date in the picker", async () => {
    const user = userEvent.setup();
    api({ "GET /api/v1/findings?limit=50": () => findings() });
    const router = renderAt("/findings");

    await user.click(await screen.findByRole("button", { name: "Viewing now" }));
    const form = screen.getByRole("form", { name: "View as of a past date" });
    await user.type(within(form).getByLabelText("View as of (UTC)"), "2999-01-01T00:00");
    await user.click(within(form).getByRole("button", { name: "View" }));

    expect(within(form).getByRole("alert")).toHaveTextContent(
      "Choose a date and time in the past.",
    );
    expect(asOfIn(router)).toBeNull();
  });

  it("keeps the date across findings and controls, and says other pages are current", async () => {
    const user = userEvent.setup();
    const calls = api({
      [`GET /api/v1/findings?limit=50&as_of=${Q}`]: () => findings(past),
      [`GET /api/v1/findings/5?as_of=${Q}`]: () =>
        json({ ...findingDetail(5, evaluation(20)), ...past }),
      [`GET /api/v1/findings/5/evaluations?limit=50&as_of=${Q}`]: () =>
        json({ items: [evaluation(20)], next_before_id: null, ...past }),
      "GET /api/v1/gitlab/projects/105/observations?before_id=10&limit=1": () =>
        json({ items: [observation(9)], next_before_id: null }),
      [EXCEPTIONS]: () => json({ items: [], next_after_id: null }),
      [`GET /api/v1/controls?framework=soc2&as_of=${Q}`]: () =>
        json({ ...controlList([control("CC8.1")]), ...past }),
      [PENDING_MAPPING]: () => json({ items: [] }),
    });
    const findingsFixture = finding(5);
    const router = renderAt(`/findings?as_of=${AS_OF}`);

    await user.click(await screen.findByRole("link", { name: findingsFixture.project.path }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "project-visibility on platform/api" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/findings/5");
    expect(asOfIn(router)).toBe(AS_OF);
    expect(calls.map((call) => call.url)).toContain(`/api/v1/findings/5?as_of=${Q}`);

    const nav = screen.getByRole("navigation", { name: "Primary" });
    await user.click(within(nav).getByRole("link", { name: "Evidence" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Evidence" })).toBeInTheDocument();
    expect(asOfIn(router)).toBeNull();
    expect(screen.getByRole("region", { name: "Past-date view" })).toHaveTextContent(
      "This page shows current data",
    );

    await user.click(within(nav).getByRole("link", { name: "Controls" }));
    expect(await screen.findByRole("link", { name: "CC8.1" })).toBeInTheDocument();
    expect(asOfIn(router)).toBe(AS_OF);
    expect(calls.map((call) => call.url)).toContain(`/api/v1/controls?framework=soc2&as_of=${Q}`);
  });

  it("warns when the server does not confirm the date", async () => {
    api({ [`GET /api/v1/findings?limit=50&as_of=${Q}`]: () => findings() });
    renderAt(`/findings?as_of=${AS_OF}`);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server did not confirm these results are as of 1 Oct 2026, 00:00 UTC; they may be current.",
    );
  });

  it("says when no evaluation had finished by the date", async () => {
    api({
      [`GET /api/v1/findings?limit=50&as_of=${Q}`]: () =>
        json({ items: [], next_after_id: null, as_of: AS_OF, evaluation_run: null }),
    });
    renderAt(`/findings?as_of=${AS_OF}`);

    expect(
      await screen.findByText(
        "No evaluation had finished by 1 Oct 2026, 00:00 UTC, so there are no results for then.",
      ),
    ).toBeInTheDocument();
  });

  it("turns off exception requests and plans on a finding viewed in the past", async () => {
    api(
      {
        [`GET /api/v1/findings/5?as_of=${Q}`]: () => json({ ...findingDetail(5), ...past }),
        [`GET /api/v1/findings/5/evaluations?limit=50&as_of=${Q}`]: () =>
          json({ items: [evaluation(20)], next_before_id: null }),
        "GET /api/v1/gitlab/projects/105/observations?before_id=10&limit=1": () =>
          json({ items: [observation(9)], next_before_id: null }),
        [EXCEPTIONS]: () => json({ items: [], next_after_id: null }),
      },
      planner,
    );
    renderAt(`/findings/5?as_of=${AS_OF}`);

    const panel = await screen.findByRole("region", { name: "Exception" });
    expect(await within(panel).findByText(/You are viewing the past/)).toBeInTheDocument();
    expect(panel).toHaveTextContent("Exceptions are shown as they are now.");
    expect(within(panel).queryByRole("button", { name: /request/i })).not.toBeInTheDocument();
    expect(
      screen.getByText(/Plan a dry-run fix for this finding: not available while viewing the past/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Plan a dry-run fix for this finding" }),
    ).not.toBeInTheDocument();
  });

  it("turns off mapping changes on a control viewed in the past", async () => {
    api({
      [`GET /api/v1/controls/soc2/CC8.1?as_of=${Q}`]: () => json({ ...controlDetail(), ...past }),
    });
    renderAt(`/controls/soc2/CC8.1?as_of=${AS_OF}`);

    const actions = await screen.findByRole("region", { name: "Adjust the mapping" });
    expect(actions).toHaveTextContent("You are viewing the past");
    for (const button of within(actions).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    const mapped = screen.getByRole("region", { name: "Rule types that count toward it" });
    for (const button of within(mapped).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
  });

  it("shows the dashboard as of the date, without today's evaluation status", async () => {
    const calls = api({
      [`GET /api/v1/evaluations/runs?limit=100&as_of=${Q}`]: () =>
        json({ items: [evaluationRun(4)], next_before_id: null, ...past }),
      [`GET /api/v1/findings?state=open&limit=200&as_of=${Q}`]: () =>
        json({ items: [finding(5)], next_after_id: null, ...past }),
    });
    renderAt(`/?as_of=${AS_OF}`);

    expect(await screen.findByText(/from evaluation run 4/)).toBeInTheDocument();
    expect(await screen.findByText("1 open findings.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Evaluation status/ })).not.toBeInTheDocument();
    expect(calls.map((call) => call.url)).toContain(
      `/api/v1/evaluations/runs?limit=100&as_of=${Q}`,
    );
  });
});
