import { describe, expect, it } from "vitest";

import {
  emptyBaseline,
  formProblems,
  fromDocument,
  readableRules,
  recommendedBaseline,
  scopeGroups,
  toDocument,
  toYaml,
} from "./yaml";

describe("baseline form and YAML", () => {
  it("writes the recommended baseline as a complete document", () => {
    expect(toYaml(recommendedBaseline())).toBe(`apiVersion: baluarte.io/v1alpha1
kind: GitLabBaseline
metadata:
  name: recommended-baseline
  description: "No public projects; every default branch protected."
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
`);
  });

  it("quotes free text and group paths so YAML can't reinterpret them", () => {
    const form = recommendedBaseline();
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
    const form = recommendedBaseline();
    form.visibility.allowed = ["internal", "private"];
    expect(toYaml(form)).toContain("allowed: [private, internal]");
  });

  it("round-trips a saved document through the form", () => {
    const form = recommendedBaseline();
    form.scope = "groups";
    form.groups = ["platform"];
    expect(fromDocument(toDocument(form))).toEqual(form);
  });

  it("opens a version the form can't show fully as YAML instead", () => {
    const doc = toDocument(recommendedBaseline()) as { spec: { rules: unknown[] } };
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
    expect(formProblems(recommendedBaseline())).toEqual([]);
  });

  it("describes rules as sentences", () => {
    const rules = readableRules(toDocument(recommendedBaseline()));
    expect(rules.map((rule) => [rule.sentence, rule.severity])).toEqual([
      ["Projects are not public", "high"],
      ["Every default branch is protected", "medium"],
    ]);
    const privateOnly = recommendedBaseline();
    privateOnly.visibility.allowed = ["private"];
    expect(readableRules(toDocument(privateOnly))[0]?.sentence).toBe("Projects are private");
  });

  it("reads a version's scope", () => {
    expect(scopeGroups(toDocument(recommendedBaseline()))).toBeNull();
    const limited = recommendedBaseline();
    limited.scope = "groups";
    limited.groups = ["platform"];
    expect(scopeGroups(toDocument(limited))).toEqual(["platform"]);
  });
});

describe("GitLab security policy rules", () => {
  function withSecurityRules() {
    const form = recommendedBaseline();
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

  it("leaves them out of the recommended and blank baselines", () => {
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
    const document = toDocument(recommendedBaseline());
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
    const unknownScan = toDocument(recommendedBaseline());
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
