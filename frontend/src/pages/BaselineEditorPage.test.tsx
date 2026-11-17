import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PolicyVersion } from "../api/policies";
import { basicBaseline, coverageFixture } from "../policies/testForms";
import { toDocument, toYaml, withRecommendedRules, type BaselineForm } from "../policies/yaml";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { evaluationFreshness, EVALUATION_FRESHNESS, freshness } from "../test/freshness";

const SAVE = "POST /api/v1/policies";
const COVERAGE = "GET /api/v1/policies/coverage";
const VERSION_2 = "GET /api/v1/policies/starter-baseline/versions/2";

function version(form: BaselineForm): PolicyVersion {
  return {
    id: 2,
    name: form.name,
    version: 2,
    yaml_digest: "2".repeat(64),
    effective_at: "2026-10-30T20:00:00Z",
    created_at: "2026-10-30T20:00:00Z",
    source_yaml: toYaml(form),
    document: toDocument(form),
  };
}

/** starter-baseline v2: visibility (private only, critical) and a custom members rule. */
function current(): BaselineForm {
  const form = basicBaseline("starter-baseline");
  form.visibility = {
    ...form.visibility,
    allowed: ["private"],
    severity: "critical",
  };
  form.members = {
    ...form.members,
    enabled: true,
    maxPrivileged: 12,
    forbidInactiveAccounts: false,
  };
  return form;
}

function api(routes: Record<string, (init?: RequestInit) => Response>): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(analyst),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [VERSION_2]: () => json(version(current())),
    [COVERAGE]: () => json(coverageFixture()),
    ...routes,
  });
}

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function savedYaml(calls: Call[]): string {
  const save = calls.find((call) => `${call.init?.method ?? "GET"} ${call.url}` === SAVE);
  return (JSON.parse(String(save?.init?.body)) as { source_yaml: string }).source_yaml;
}

describe("baseline editor: recommended rules", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("leaves the security policy rules off, and says why, when the instance doesn't report them", async () => {
    api({});
    renderAt("/governance/baselines/new?start=recommended");

    expect(
      await screen.findByText(
        "The two GitLab security policy rules are off: they need GitLab Ultimate, and this instance does not report security policies.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Privileged members rule" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Changes reviewed rule" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Required scans rule" })).not.toBeInTheDocument();
  });

  it("switches the security policy rules on when the instance reports them", async () => {
    api({
      [COVERAGE]: () => json(coverageFixture({ securityPolicies: true })),
    });
    renderAt("/governance/baselines/new?start=recommended");

    expect(await screen.findByRole("region", { name: "Required scans rule" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Required approvals rule" })).toBeInTheDocument();
    expect(screen.queryByText(/need GitLab Ultimate/)).not.toBeInTheDocument();
  });

  it("prefills the current version plus the missing rules, marked added, and keeps existing settings", async () => {
    const calls = api({
      [SAVE]: () => json(version(current()), 201),
      "GET /api/v1/policies/starter-baseline/versions": () => json([]),
      "GET /api/v1/policy-assignments?policy_name=starter-baseline&limit=100": () =>
        json({ items: [], next_after_id: null }),
      "GET /api/v1/evaluations/summary?policy=starter-baseline": () =>
        json({
          evaluation_run: null,
          freshness: evaluationFreshness(),
          baselines: [],
        }),
    });
    const user = userEvent.setup();
    renderAt("/governance/baselines/new?from=starter-baseline&version=2&add=recommended");

    expect(
      await screen.findByRole("heading", {
        name: "Add recommended rules to starter-baseline",
      }),
    ).toBeInTheDocument();
    const notice = await screen.findByText(/^Recommended rules added, marked Added:/);
    expect(notice).toHaveTextContent(
      "Project merge request approvals, Group two-factor authentication, Project access tokens, Changes reviewed, Pipelines passed, Deployed after review.",
    );
    expect(screen.getByText(/keep their settings/)).toBeInTheDocument();

    for (const name of [
      "Project approval settings rule",
      "Two-factor authentication rule",
      "Access token lifetime rule",
      "Changes reviewed rule",
      "Change pipelines rule",
      "Deployments reviewed rule",
    ]) {
      expect(within(screen.getByRole("region", { name })).getByText("Added")).toBeInTheDocument();
    }
    const members = screen.getByRole("region", {
      name: "Privileged members rule",
    });
    expect(within(members).queryByText("Added")).not.toBeInTheDocument();
    expect(
      within(members).getByRole("spinbutton", {
        name: "Members with Maintainer or Owner access, at most",
      }),
    ).toHaveValue(12);
    expect(screen.getByRole("checkbox", { name: "Private" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Internal" })).not.toBeChecked();
    // Visibility and branch protection were already on: not marked.
    expect(screen.getAllByText("Added")).toHaveLength(6);

    await user.click(screen.getByRole("button", { name: "Save new version" }));

    await vi.waitFor(() => {
      expect(calls.some((call) => `${call.init?.method ?? "GET"} ${call.url}` === SAVE)).toBe(true);
    });
    const yaml = savedYaml(calls);
    expect(yaml).toContain(`      allowed: [private]
      severity: critical`);
    expect(yaml).toContain(`      max_privileged: 12
      forbid_inactive_accounts: false`);
    expect(yaml).toContain("type: gitlab.change.deployed_after_review");
    expect(yaml).toContain("type: gitlab.group.two_factor_required");
    expect(yaml).not.toContain("security_policy");
  });

  it("says so when the version already has every recommended rule", async () => {
    const full = withRecommendedRules(current(), { securityPolicies: false }).form;
    api({ [VERSION_2]: () => json(version(full)) });
    renderAt("/governance/baselines/new?from=starter-baseline&version=2&add=recommended");

    expect(
      await screen.findByText("Version 2 already has every recommended rule."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Added")).not.toBeInTheDocument();
  });

  it("opens a version the form can't show as YAML, without adding rules", async () => {
    const odd = version(current());
    (odd.document.spec as { rules: unknown[] }).rules.push({
      id: "labels",
      type: "gitlab.labels.consistent",
    });
    api({ [VERSION_2]: () => json(odd) });
    renderAt("/governance/baselines/new?from=starter-baseline&version=2&add=recommended");

    expect(await screen.findByText(/recommended rules can't be added for you/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Baseline YAML" })).toHaveValue(odd.source_yaml);
  });
});
