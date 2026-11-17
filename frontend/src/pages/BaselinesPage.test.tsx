import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import type { Coverage } from "../policies/coverage";
import { coverageFixture } from "../policies/testForms";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { evaluationFreshness, EVALUATION_FRESHNESS, freshness } from "../test/freshness";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:export", "evidence:read", "findings:read", "inventory:read"],
};

function api(user: CurrentUser, coverage: () => Response) {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/policies": () =>
      json([
        {
          name: "starter-baseline",
          latest_version: 2,
          versions: 2,
          latest_effective_at: "2026-10-30T20:00:00Z",
        },
      ]),
    "GET /api/v1/policy-assignments?limit=100": () =>
      json({
        items: [
          {
            id: 1,
            policy_id: 2,
            policy_name: "starter-baseline",
            version: 2,
            yaml_digest: "2".repeat(64),
            target_type: "instance",
            target_id: 1,
            target_path: "https://gitlab.example.test",
            target_state: null,
            created_at: "2026-10-30T20:05:00Z",
          },
        ],
        next_after_id: null,
      }),
    "GET /api/v1/evaluations/summary": () =>
      json({
        evaluation_run: null,
        freshness: evaluationFreshness(),
        baselines: [],
      }),
    "GET /api/v1/policies/coverage": coverage,
  });
}

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function asPhone() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("max-width: 767.98px"),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

describe("baselines: rule coverage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("counts rule types in force and names the missing ones with what they prove", async () => {
    api(auditor, () => json(coverageFixture()));
    renderAt("/governance");

    const card = await screen.findByRole("region", { name: "Rule coverage" });
    expect(await within(card).findByText("2 of 11 rule types in force")).toBeInTheDocument();
    const missing = within(card).getByRole("list", { name: "Not in force" });
    const items = within(missing).getAllByRole("listitem");
    expect(items).toHaveLength(9);
    expect(missing).toHaveTextContent("Privileged members");
    expect(missing).toHaveTextContent(
      "Maintainer and Owner access is limited, and no inactive account keeps it.",
    );
    expect(missing).not.toHaveTextContent("Project visibility");
    expect(missing).not.toHaveTextContent("Label consistency");
    const scan = items.find((item) => item.textContent.includes("Scan execution policy"));
    expect(scan).toHaveTextContent(
      "Needs GitLab Ultimate; this instance does not report security policies.",
    );
    expect(card).toHaveTextContent("Label consistency: not in force");
    expect(card).toHaveTextContent("there is none yet");
  });

  it("lists controls without automated evidence behind a disclosure, linked to each control", async () => {
    api(auditor, () => json(coverageFixture()));
    const user = userEvent.setup();
    renderAt("/governance");

    const card = await screen.findByRole("region", { name: "Rule coverage" });
    const summary = await within(card).findByText(
      "2 controls have no automated evidence from Baluarte",
    );
    await user.click(summary);
    expect(within(card).getByRole("link", { name: "NIST 800-53 RA-5" })).toHaveAttribute(
      "href",
      "/controls/nist_800_53_moderate/RA-5",
    );
    expect(within(card).getByRole("link", { name: "NIST 800-53 IA-2(1)" })).toHaveAttribute(
      "href",
      "/controls/nist_800_53_moderate/IA-2(1)",
    );
    expect(card).toHaveTextContent("(met outside GitLab)");
    expect(card).toHaveTextContent("They still need attestation or other evidence.");
  });

  it("says when every rule type is in force", async () => {
    const full: Coverage = coverageFixture();
    full.rule_types = full.rule_types.map((item) => ({
      ...item,
      in_force: true,
    }));
    full.controls = [];
    full.label_taxonomy_version = 3;
    api(auditor, () => json(full));
    renderAt("/governance");

    const card = await screen.findByRole("region", { name: "Rule coverage" });
    expect(await within(card).findByText("11 of 11 rule types in force")).toBeInTheDocument();
    expect(card).toHaveTextContent("Every rule type is used by an applied baseline.");
    expect(card).toHaveTextContent("Label consistency: in force");
    expect(card).toHaveTextContent("label taxonomy v3");
    expect(card).toHaveTextContent("Every mapped control has automated evidence");
  });

  it("keeps the list when coverage can't be read", async () => {
    api(auditor, () => json({ detail: "boom" }, 500));
    renderAt("/governance");

    expect(await screen.findByText("Coverage is unavailable right now.")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Baselines" })).toBeInTheDocument();
  });

  it("offers to add the recommended rules a baseline lacks, to policy writers", async () => {
    api(analyst, () => json(coverageFixture()));
    renderAt("/governance");

    const link = await screen.findByRole("link", {
      name: "Add recommended rules to starter-baseline",
    });
    expect(link).toHaveAttribute(
      "href",
      "/governance/baselines/new?from=starter-baseline&version=2&add=recommended",
    );
    expect(screen.getByText("(7 missing)")).toBeInTheDocument();
  });

  it("counts the security policy rules as missing only where the instance reports them", async () => {
    api(analyst, () => json(coverageFixture({ securityPolicies: true })));
    renderAt("/governance");

    await screen.findByRole("link", {
      name: "Add recommended rules to starter-baseline",
    });
    expect(screen.getByText("(9 missing)")).toBeInTheDocument();
  });

  it("hides the action when the baseline has every recommended rule", async () => {
    const complete = coverageFixture();
    complete.baselines = complete.baselines.map((baseline) => ({
      ...baseline,
      rule_types: complete.rule_types.map((item) => item.rule_type),
    }));
    api(analyst, () => json(complete));
    renderAt("/governance");
    await screen.findByText("2 of 11 rule types in force");
    expect(screen.queryByRole("link", { name: /Add recommended rules/ })).not.toBeInTheDocument();
  });

  it("hides the action from readers", async () => {
    api(auditor, () => json(coverageFixture()));
    renderAt("/governance");
    await screen.findByText("2 of 11 rule types in force");
    expect(screen.queryByRole("link", { name: /Add recommended rules/ })).not.toBeInTheDocument();
  });

  it("shows the card read-only on a phone", async () => {
    asPhone();
    api(analyst, () => json(coverageFixture()));
    renderAt("/governance");

    const card = await screen.findByRole("region", { name: "Rule coverage" });
    expect(await within(card).findByText("2 of 11 rule types in force")).toBeInTheDocument();
    expect(within(card).queryByRole("button")).not.toBeInTheDocument();
    expect(card.querySelector("table")).toBeNull();
    expect(screen.queryByRole("link", { name: /Add recommended rules/ })).not.toBeInTheDocument();
  });
});
