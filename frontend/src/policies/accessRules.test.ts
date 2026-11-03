import { describe, expect, it } from "vitest";

import {
  emptyBaseline,
  formProblems,
  fromDocument,
  readableRules,
  recommendedBaseline,
  toDocument,
  toYaml,
} from "./yaml";

function withAccessRules() {
  const form = recommendedBaseline();
  form.projectApprovals.enabled = true;
  form.members.enabled = true;
  form.twoFactor.enabled = true;
  form.tokens = { ...form.tokens, enabled: true, maxUnusedDays: 30 };
  return form;
}

describe("access-control and project approval rules", () => {
  it("leaves them out of the recommended and blank baselines", () => {
    for (const form of [recommendedBaseline(), emptyBaseline()]) {
      expect(form.projectApprovals.enabled).toBe(false);
      expect(form.members.enabled).toBe(false);
      expect(form.twoFactor.enabled).toBe(false);
      expect(form.tokens.enabled).toBe(false);
    }
    const yaml = toYaml(recommendedBaseline());
    expect(yaml).not.toContain("merge_request_approvals");
    expect(yaml).not.toContain("access_tokens");
  });

  it("writes them in the contract's shape, leaving out unset optional limits", () => {
    const form = withAccessRules();
    form.twoFactor.maxGraceHours = null;
    form.members.description = "Reviewed quarterly by auditor.";
    expect(toYaml(form)).toContain(`    - id: merge-request-approvals
      type: gitlab.project.merge_request_approvals
      min_approvals: 1
      forbid_author_approval: true
      forbid_committer_approval: false
      reset_approvals_on_push: true
      forbid_override: true
      severity: high
    - id: privileged-members
      type: gitlab.project.privileged_members
      max_privileged: 5
      forbid_inactive_accounts: true
      severity: medium
      description: "Reviewed quarterly by auditor."
    - id: two-factor-required
      type: gitlab.group.two_factor_required
      severity: high
    - id: access-token-lifetime
      type: gitlab.project.access_tokens
      max_lifetime_days: 90
      max_unused_days: 30
      severity: medium
`);
  });

  it("round-trips through the form, with and without optional limits", () => {
    const form = withAccessRules();
    form.projectApprovals.minApprovals = 0;
    form.tokens.description = "Rotate acme tokens.";
    expect(fromDocument(toDocument(form))).toEqual(form);
    form.twoFactor.maxGraceHours = null;
    form.tokens.maxUnusedDays = null;
    const document = toDocument(form);
    const rules = (document.spec as { rules: Record<string, unknown>[] }).rules;
    expect(rules.find((rule) => rule.id === "two-factor-required")).not.toHaveProperty(
      "max_grace_hours",
    );
    expect(fromDocument(document)).toEqual(form);
  });

  it("applies the contract's defaults when optional fields are left out", () => {
    const document = toDocument(recommendedBaseline());
    (document.spec as { rules: unknown[] }).rules.push(
      { id: "approvals", type: "gitlab.project.merge_request_approvals", min_approvals: 2 },
      { id: "members", type: "gitlab.project.privileged_members", max_privileged: 3 },
      { id: "mfa", type: "gitlab.group.two_factor_required" },
      { id: "tokens", type: "gitlab.project.access_tokens", max_lifetime_days: 30 },
    );
    const form = fromDocument(document);
    expect(form?.projectApprovals).toMatchObject({
      enabled: true,
      minApprovals: 2,
      forbidAuthorApproval: false,
      forbidCommitterApproval: false,
      resetApprovalsOnPush: false,
      forbidOverride: false,
    });
    expect(form?.members).toMatchObject({ maxPrivileged: 3, forbidInactiveAccounts: false });
    expect(form?.twoFactor).toMatchObject({ enabled: true, maxGraceHours: null });
    expect(form?.tokens).toMatchObject({ maxLifetimeDays: 30, maxUnusedDays: null });
  });

  it("opens as YAML when the form can't show the rule exactly", () => {
    const cases: Record<string, unknown>[] = [
      { id: "a", type: "gitlab.project.merge_request_approvals", min_approvals: 1.5 },
      { id: "b", type: "gitlab.project.privileged_members", max_privileged: "5" },
      { id: "c", type: "gitlab.group.two_factor_required", max_grace_hours: "48" },
      {
        id: "d",
        type: "gitlab.project.access_tokens",
        max_lifetime_days: 30,
        max_unused_days: 2.5,
      },
      {
        id: "e",
        type: "gitlab.project.merge_request_approvals",
        min_approvals: 1,
        forbid_override: "yes",
      },
    ];
    for (const rule of cases) {
      const document = toDocument(recommendedBaseline());
      (document.spec as { rules: unknown[] }).rules.push(rule);
      expect(fromDocument(document)).toBeNull();
    }
    const twice = toDocument(withAccessRules());
    (twice.spec as { rules: unknown[] }).rules.push({
      id: "more-members",
      type: "gitlab.project.privileged_members",
      max_privileged: 2,
    });
    expect(fromDocument(twice)).toBeNull();
  });

  it("checks each limit's range before saving", () => {
    const form = withAccessRules();
    expect(formProblems(form)).toEqual([]);
    form.projectApprovals.minApprovals = 21;
    form.members.maxPrivileged = 0;
    form.twoFactor.maxGraceHours = 721;
    form.tokens.maxLifetimeDays = Number.NaN;
    form.tokens.maxUnusedDays = 366;
    expect(formProblems(form)).toEqual([
      "Project approvals: enter a whole number from 0 to 20.",
      "Privileged members: enter a whole number from 1 to 1000.",
      "Two-factor grace period: enter a whole number from 0 to 720, or leave it empty.",
      "Access token lifetime: enter a whole number from 1 to 365.",
      "Unused access tokens: enter a whole number from 1 to 365, or leave it empty.",
    ]);
    form.projectApprovals.minApprovals = 0;
    form.members.maxPrivileged = 1000;
    form.twoFactor.maxGraceHours = 0;
    form.tokens.maxLifetimeDays = 365;
    form.tokens.maxUnusedDays = 1;
    expect(formProblems(form)).toEqual([]);
    const only = emptyBaseline();
    only.name = "two-factor-only";
    only.twoFactor = { ...only.twoFactor, enabled: true, maxGraceHours: null };
    expect(formProblems(only)).toEqual([]);
  });

  it("describes them as sentences", () => {
    const form = withAccessRules();
    form.members.maxPrivileged = 1;
    expect(readableRules(toDocument(form)).slice(2)).toEqual([
      {
        id: "merge-request-approvals",
        sentence:
          "Merge requests to the default branch need at least 1 approval under the project's approval rules",
        detail:
          "Also: authors can't approve their own merge requests, a new push removes approvals and a merge request can't change its approval rules.",
        severity: "high",
      },
      {
        id: "privileged-members",
        sentence: "At most 1 member has Maintainer or Owner access to a project",
        detail: "No blocked or deactivated account keeps access.",
        severity: "medium",
      },
      {
        id: "two-factor-required",
        sentence:
          "A group above every project requires two-factor authentication with a grace period of at most 48 hours",
        detail: null,
        severity: "high",
      },
      {
        id: "access-token-lifetime",
        sentence: "Project access tokens expire within 90 days of being created",
        detail: "Each active token was used in the last 30 days.",
        severity: "medium",
      },
    ]);
  });
});
