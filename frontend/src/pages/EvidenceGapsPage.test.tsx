import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GapReport } from "../api/evidenceGaps";
import { routes } from "../routes";
import { lastQuarter } from "./EvidenceGapsPage";
import { analyst, json, mockApi } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const report: GapReport = {
  framework: "soc2",
  version: "2017-r2022",
  period_start: "2026-07-01",
  period_end: "2026-09-30",
  evaluation_run_id: 4,
  collected_at: "2026-09-30T12:00:00Z",
  summary: { missing: 1, stale: 0, inaccessible: 0, incomplete: 1 },
  controls: [
    {
      control_id: "CC6.1",
      title: "Logical access",
      area: "Access",
      status: "unknown",
      gaps: ["missing", "incomplete"],
      reasons: [
        "No accepted evidence item and no collected result",
        "No approved assessment covers the period",
      ],
      accepted_items: 0,
      fresh_items: 0,
      collected_results: 0,
      assessment_id: null,
      conclusion: null,
    },
    {
      control_id: "CC8.1",
      title: "Change management",
      area: "Change",
      status: "met",
      gaps: [],
      reasons: [],
      accepted_items: 2,
      fresh_items: 2,
      collected_results: 12,
      assessment_id: 7,
      conclusion: "effective",
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("lastQuarter", () => {
  it("is the last full calendar quarter", () => {
    expect(lastQuarter(new Date("2026-11-27T00:00:00Z"))).toEqual({
      start: "2026-07-01",
      end: "2026-09-30",
    });
    expect(lastQuarter(new Date("2026-02-10T00:00:00Z"))).toEqual({
      start: "2025-10-01",
      end: "2025-12-31",
    });
  });
});

describe("EvidenceGapsPage", () => {
  it("lists controls with a gap, and all controls on request", async () => {
    const user = userEvent.setup();
    const period = lastQuarter(new Date());
    const calls = mockApi({
      "GET /api/v1/auth/me": () => json(analyst),
      "GET /api/v1/gitlab/freshness": () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
      [`GET /api/v1/evidence-gaps?framework=soc2&period_start=${period.start}&period_end=${period.end}`]:
        () => json(report),
    });
    const router = createMemoryRouter(routes, { initialEntries: ["/evidence-gaps"] });
    render(<RouterProvider router={router} />);

    const table = await screen.findByRole("table", { name: "Evidence gaps" });
    expect(within(table).getByRole("link", { name: "CC6.1" })).toHaveAttribute(
      "href",
      "/controls/soc2/CC6.1",
    );
    expect(within(table).queryByRole("link", { name: "CC8.1" })).not.toBeInTheDocument();
    expect(screen.getByText("Missing: 1")).toBeInTheDocument();
    const request = calls.find((c) => c.url.startsWith("/api/v1/evidence-gaps"));
    expect(request?.url).toContain("framework=soc2");

    await user.selectOptions(screen.getByLabelText("Show"), "");
    expect(within(table).getByRole("link", { name: "CC8.1" })).toBeInTheDocument();
    expect(within(table).getByText("Assessment 7")).toBeInTheDocument();
  });
});
