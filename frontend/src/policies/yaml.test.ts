import { describe, expect, it } from "vitest";

import {
  emptyBaseline,
  formProblems,
  fromDocument,
  readableRules,
  recommendedBaseline,
  ruleKeys,
  scopeGroups,
  toDocument,
  toYaml,
  withRecommendedRules,
} from "./yaml";
import { basicBaseline } from "./testForms";

describe("the recommended baseline", () => {
  it("switches on every rule that works on any GitLab tier", () => {
    expect(toYaml(recommendedBaseline())).toBe(`apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: recommended-baseline
  description: "No public projects; default branches protected; merge requests approved; privileged access, two-factor and access tokens limited; changes reviewed, tested and deployed after review."
spec:
  scope:
    groups: ["*"]
  rules:
    - id: project-visibility
      type: gitlab.project.visibility
      allowed: [private, internal]
      severity: high
      description: "Projects must not be public."
    - id: protected-default-branch
      type: gitlab.branch.default_protected
      required: true
      severity: medium
      description: "The default branch must be covered by a protected-branch rule."
    - id: merge-request-approvals
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
    - id: two-factor-required
      type: gitlab.group.two_factor_required
      max_grace_hours: 48
      severity: high
    - id: access-token-lifetime
      type: gitlab.project.access_tokens
      max_lifetime_days: 90
      severity: medium
    - id: changes-reviewed
      type: gitlab.change.reviewed
      window_days: 90
      min_approvals: 1
      forbid_self_approval: true
      allow_direct_push: false
      severity: high
    - id: changes-pipeline-passed
      type: gitlab.change.pipeline_passed
      window_days: 90
      require_tests: false
      severity: medium
    - id: deployments-reviewed
      type: gitlab.change.deployed_after_review
      window_days: 90
      severity: high
`);
    expect(formProblems(recommendedBaseline())).toEqual([]);
  });

  it("adds the GitLab security policy rules only when the instance reports security policies", () => {
    const without = recommendedBaseline({ securityPolicies: false });
    expect([without.scan.enabled, without.approval.enabled]).toEqual([false, false]);
    expect(toYaml(without)).not.toContain("security_policy");
    const withPolicies = recommendedBaseline({ securityPolicies: true });
    expect([withPolicies.scan.enabled, withPolicies.approval.enabled]).toEqual([true, true]);
    const yaml = toYaml(withPolicies);
    expect(yaml).toContain(`    - id: required-scans
      type: gitlab.security_policy.scan_execution
      scans: [secret_detection, sast]
      allow_skip_ci: true
      severity: high
    - id: required-approvals
      type: gitlab.security_policy.approval
      min_approvals: 1`);
    expect(withPolicies.description).toContain("GitLab scan execution and approval policies");
    expect(fromDocument(toDocument(withPolicies))).toEqual(withPolicies);
  });

  it("adds the recommended rules a version lacks without changing the others", () => {
    const current = basicBaseline("acme-baseline");
    current.visibility.allowed = ["private"];
    current.branch.severity = "critical";
    current.members = {
      ...current.members,
      enabled: true,
      maxPrivileged: 12,
      forbidInactiveAccounts: false,
    };
    // An existing rule already uses an ID a recommended rule would take.
    current.visibility.id = "changes-reviewed";

    const { form, added } = withRecommendedRules(current, { securityPolicies: false });

    expect(added).toEqual([
      "projectApprovals",
      "twoFactor",
      "tokens",
      "reviewed",
      "pipeline",
      "deployed",
    ]);
    expect(form.visibility).toEqual(current.visibility);
    expect(form.branch).toEqual(current.branch);
    expect(form.members).toEqual(current.members);
    expect(form.name).toBe("acme-baseline");
    expect(form.reviewed).toMatchObject({ enabled: true, id: "changes-reviewed-2" });
    expect(form.scan.enabled).toBe(false);
    const ids = ruleKeys.filter((key) => form[key].enabled).map((key) => form[key].id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(formProblems(form)).toEqual([]);

    const all = withRecommendedRules(form, { securityPolicies: true });
    expect(all.added).toEqual(["scan", "approval"]);
    expect(withRecommendedRules(all.form, { securityPolicies: true }).added).toEqual([]);
  });
});

describe("baseline form and YAML", () => {
  it("quotes free text and group paths so YAML can't reinterpret them", () => {
    const form = basicBaseline();
    form.description = 'yes: "really" # not a comment';
    form.scope = "groups";
    form.groups = [" platform/ ", "", "platform", "finance/ledger"];
    form.branch.enabled = false;
    const yaml = toYaml(form);
    expect(yaml).toContain('  description: "yes: \\"really\\" # not a comment"');
    expect(yaml).toContain('    groups: ["platform", "finance/ledger"]');
    expect(yaml).not.toContain("default_protected");
  });

  it("orders allowed visibility the same way however it was ticked", () => {
    const form = basicBaseline();
    form.visibility.allowed = ["internal", "private"];
    expect(toYaml(form)).toContain("allowed: [private, internal]");
  });

  it("round-trips a saved document through the form", () => {
    const form = basicBaseline();
    form.scope = "groups";
    form.groups = ["platform"];
    expect(fromDocument(toDocument(form))).toEqual(form);
  });

  it("opens a version the form can't show fully as YAML instead", () => {
    const doc = toDocument(basicBaseline()) as { spec: { rules: unknown[] } };
    const twice = { ...doc, spec: { ...doc.spec, rules: [...doc.spec.rules, doc.spec.rules[0]] } };
    expect(fromDocument(twice)).toBeNull();
    expect(
      fromDocument({
        ...doc,
        spec: { ...doc.spec, rules: [{ id: "x", type: "gitlab.future.rule" }] },
      }),
    ).toBeNull();
    expect(fromDocument({})).toBeNull();
  });

  it("lists what must be fixed before saving", () => {
    const form = emptyBaseline();
    expect(formProblems(form)).toEqual([
      expect.stringContaining("Name:"),
      "Rules: turn on at least one rule.",
    ]);
    form.name = "ok-name";
    form.visibility.enabled = true;
    form.visibility.allowed = [];
    form.scope = "groups";
    form.groups = [" "];
    expect(formProblems(form)).toEqual([
      "Groups: enter at least one group path, or choose every group.",
      "Project visibility: allow at least one visibility.",
    ]);
    expect(formProblems(basicBaseline())).toEqual([]);
  });

  it("describes rules as sentences", () => {
    const rules = readableRules(toDocument(basicBaseline()));
    expect(rules.map((rule) => [rule.sentence, rule.severity])).toEqual([
      ["Projects are not public", "high"],
      ["Every default branch is protected", "medium"],
    ]);
    const privateOnly = basicBaseline();
    privateOnly.visibility.allowed = ["private"];
    expect(readableRules(toDocument(privateOnly))[0]?.sentence).toBe("Projects are private");
  });

  it("reads a version's scope", () => {
    expect(scopeGroups(toDocument(basicBaseline()))).toBeNull();
    const limited = basicBaseline();
    limited.scope = "groups";
    limited.groups = ["platform"];
    expect(scopeGroups(toDocument(limited))).toEqual(["platform"]);
  });
});

describe("GitLab security policy rules", () => {
  function withSecurityRules() {
    const form = basicBaseline();
    form.scan = {
      ...form.scan,
      enabled: true,
      scans: ["sast", "secret_detection"],
    };
    form.approval = {
      ...form.approval,
      enabled: true,
      minApprovals: 2,
      forbidAuthorApproval: true,
    };
    return form;
  }

  it("leaves them out of the default recommended and blank baselines", () => {
    expect(recommendedBaseline().scan.enabled).toBe(false);
    expect(recommendedBaseline().approval.enabled).toBe(false);
    expect(emptyBaseline().scan.enabled).toBe(false);
    expect(toYaml(recommendedBaseline())).not.toContain("security_policy");
  });

  it("writes them in the contract's shape", () => {
    expect(toYaml(withSecurityRules())).toContain(`    - id: required-scans
      type: gitlab.security_policy.scan_execution
      scans: [secret_detection, sast]
      allow_skip_ci: true
      severity: high
    - id: required-approvals
      type: gitlab.security_policy.approval
      min_approvals: 2
      forbid_author_approval: true
      forbid_committer_approval: false
      severity: medium
`);
  });

  it("round-trips through the form", () => {
    const form = withSecurityRules();
    form.scan.allowSkipCi = false;
    form.scan.description = "Scans on every default branch.";
    const back = fromDocument(toDocument(form));
    expect(back?.scan).toEqual({
      ...form.scan,
      scans: ["secret_detection", "sast"],
    });
    expect(back?.approval).toEqual(form.approval);
  });

  it("applies the contract's defaults when optional fields are left out", () => {
    const document = toDocument(basicBaseline());
    (document.spec as { rules: unknown[] }).rules.push(
      {
        id: "scans",
        type: "gitlab.security_policy.scan_execution",
        scans: ["dast"],
      },
      {
        id: "approvals",
        type: "gitlab.security_policy.approval",
        min_approvals: 1,
      },
    );
    const form = fromDocument(document);
    expect(form?.scan).toMatchObject({
      enabled: true,
      scans: ["dast"],
      allowSkipCi: true,
    });
    expect(form?.approval).toMatchObject({
      enabled: true,
      forbidAuthorApproval: false,
      forbidCommitterApproval: false,
    });
  });

  it("opens as YAML when the form can't show the rule exactly", () => {
    const unknownScan = toDocument(basicBaseline());
    (unknownScan.spec as { rules: unknown[] }).rules.push({
      id: "scans",
      type: "gitlab.security_policy.scan_execution",
      scans: ["fuzzing"],
    });
    expect(fromDocument(unknownScan)).toBeNull();
    const twoApprovals = toDocument(withSecurityRules());
    (twoApprovals.spec as { rules: unknown[] }).rules.push({
      id: "more",
      type: "gitlab.security_policy.approval",
      min_approvals: 3,
    });
    expect(fromDocument(twoApprovals)).toBeNull();
  });

  it("checks them before saving", () => {
    const form = withSecurityRules();
    form.scan.scans = [];
    form.approval.minApprovals = 21;
    expect(formProblems(form)).toEqual([
      "Required scans: choose at least one scan.",
      "Required approvals: enter a whole number from 1 to 20.",
    ]);
    const only = emptyBaseline();
    only.name = "approvals-only";
    only.approval.enabled = true;
    expect(formProblems(only)).toEqual([]);
  });

  it("describes them as sentences", () => {
    const form = withSecurityRules();
    form.scan.allowSkipCi = false;
    form.approval.forbidCommitterApproval = true;
    const rules = readableRules(toDocument(form)).slice(2);
    expect(rules).toEqual([
      {
        id: "required-scans",
        sentence:
          "Projects run secret detection and SAST on the default branch through a GitLab scan execution policy",
        detail:
          "Only policies that forbid skipping CI count; one without a skip_ci setting can be skipped.",
        severity: "high",
      },
      {
        id: "required-approvals",
        sentence:
          "Every merge request to a protected branch needs at least 2 approvals from a GitLab approval policy",
        detail:
          "The policy must stop the author or anyone who committed to it from approving a merge request.",
        severity: "medium",
      },
    ]);
  });
});

describe("the release practice rule", () => {
  it("is off in the recommended baseline, since it needs a practice assigned", () => {
    expect(recommendedBaseline({ securityPolicies: true }).release.enabled).toBe(false);
  });

  it("round-trips through the form, is checked and reads as a sentence", () => {
    const form = basicBaseline();
    form.release = { ...form.release, enabled: true, windowDays: 30 };
    const document = toDocument(form);
    expect(fromDocument(document)).toEqual(form);
    expect(toYaml(form)).toContain("type: gitlab.release.follows_practice");
    expect(readableRules(document).at(-1)?.sentence).toBe(
      "Every production release over the last 30 days followed the project's release practice",
    );
    form.release.windowDays = 400;
    expect(formProblems(form)).toEqual([
      "Release practice window: enter a whole number from 1 to 365.",
    ]);
  });
});
