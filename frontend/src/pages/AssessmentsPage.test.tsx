import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Assessment } from "../api/assessments";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const LIST = "GET /api/v1/assessments";
const NARRATIVE = "Sampled 25 changes; each had an approved merge request.";

const writer: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "assessments:write"],
};
const reviewer: CurrentUser = {
  ...analyst,
  user_id: 2,
  roles: ["auditor"],
  permissions: [...analyst.permissions, "assessments:write", "assessments:review"],
};

const assessment: Assessment = {
  id: 7,
  control: {
    framework: "soc2",
    version: "2017-r2022",
    control_id: "CC8.1",
    title: "Change management",
  },
  period_start: "2026-07-01",
  period_end: "2026-09-30",
  conclusion: "effective",
  narrative: NARRATIVE,
  state: "draft",
  supersedes_id: null,
  superseded_by_id: null,
  assessor: { user_id: 1, display_name: "Ada" },
  created_at: "2026-11-27T10:00:00Z",
  submitted_at: null,
  reviewed_by: null,
  reviewed_at: null,
  review_note: null,
  development_review: false,
  citations: [],
};

function api(user: CurrentUser, routes: Record<string, (init?: RequestInit) => Response> = {}) {
  return mockApi({
    [ME]: () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [LIST]: () => json({ items: [assessment] }),
    ...routes,
  });
}

function renderPage() {
  const router = createMemoryRouter(routes, { initialEntries: ["/assessments"] });
  render(<RouterProvider router={router} />);
}

function bodyOf(calls: Call[], key: string): unknown {
  const call = calls.find((c) => `${c.init?.method ?? "GET"} ${c.url}` === key);
  return JSON.parse(String(call?.init?.body));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AssessmentsPage", () => {
  it("starts an assessment, cites the automated status and submits it", async () => {
    const user = userEvent.setup();
    let current = assessment;
    const calls = api(writer, {
      "POST /api/v1/assessments": () => json(assessment, 201),
      "GET /api/v1/assessments/7": () => json(current),
      "POST /api/v1/assessments/7/citations": () => {
        current = {
          ...assessment,
          citations: [
            {
              id: 3,
              kind: "control_status",
              target_id: null,
              title: "Automated status: met",
              snapshot: { status: "met" },
              digest: "b".repeat(64),
              rationale: "The quarter's automated status.",
              cited_at: "2026-11-27T10:05:00Z",
            },
          ],
        };
        return json(current, 201);
      },
      "POST /api/v1/assessments/7/submit": () => {
        current = { ...current, state: "submitted" };
        return json(current);
      },
    });
    renderPage();

    const form = await screen.findByRole("form", { name: "Assess a control" });
    await user.type(within(form).getByLabelText("Control"), "CC8.1");
    await user.type(within(form).getByLabelText("Period from"), "2026-07-01");
    await user.type(within(form).getByLabelText("Period to"), "2026-09-30");
    await user.type(within(form).getByLabelText("What was tested and how"), NARRATIVE);
    await user.click(within(form).getByRole("button", { name: "Start as draft" }));
    expect(await screen.findByText("Started an assessment of CC8.1.")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/assessments")).toMatchObject({
      framework: "soc2",
      control_id: "CC8.1",
      conclusion: "effective",
    });

    const cite = await screen.findByRole("form", { name: "Cite evidence" });
    await user.selectOptions(within(cite).getByLabelText("Evidence"), "control_status");
    expect(within(cite).queryByLabelText("ID")).not.toBeInTheDocument();
    await user.type(
      within(cite).getByLabelText("Why it supports the conclusion"),
      "The quarter's automated status.",
    );
    await user.click(within(cite).getByRole("button", { name: "Cite" }));
    expect(await screen.findByText("Automated status: met")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/assessments/7/citations")).toEqual({
      kind: "control_status",
      target_id: null,
      rationale: "The quarter's automated status.",
    });

    await user.click(screen.getByRole("button", { name: "Submit for review" }));
    expect(await screen.findByText("Submitted for review.")).toBeInTheDocument();
  });

  it("lets a reviewer approve someone else's assessment but not their own", async () => {
    const user = userEvent.setup();
    const waiting: Assessment = { ...assessment, state: "submitted" };
    const calls = api(reviewer, {
      [LIST]: () => json({ items: [waiting] }),
      "GET /api/v1/assessments/7": () => json(waiting),
      "POST /api/v1/assessments/7/approve": () =>
        json({ ...waiting, state: "approved", reviewed_by: { user_id: 2, display_name: "Bea" } }),
    });
    renderPage();

    const list = await screen.findByRole("table", { name: "Assessments" });
    await user.click(within(list).getByRole("button", { name: "Open assessment 7" }));
    const detail = await screen.findByRole("region", {
      name: "Assessment of SOC 2 CC8.1 (2017-r2022)",
    });
    expect(within(detail).getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.click(within(detail).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Approved.")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/assessments/7/approve")).toEqual({ note: "" });
  });

  it("tells a reviewer that someone else reviews their own assessment", async () => {
    const user = userEvent.setup();
    const own: Assessment = {
      ...assessment,
      state: "submitted",
      assessor: { user_id: 2, display_name: "Bea" },
    };
    api(reviewer, {
      [LIST]: () => json({ items: [own] }),
      "GET /api/v1/assessments/7": () => json(own),
    });
    renderPage();

    const list = await screen.findByRole("table", { name: "Assessments" });
    await user.click(within(list).getByRole("button", { name: "Open assessment 7" }));
    expect(await screen.findByText("Someone else reviews it")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("offers no assessing to those without assessments:write", async () => {
    api(analyst);
    renderPage();

    await screen.findByRole("table", { name: "Assessments" });
    expect(screen.queryByRole("form", { name: "Assess a control" })).not.toBeInTheDocument();
  });
});
