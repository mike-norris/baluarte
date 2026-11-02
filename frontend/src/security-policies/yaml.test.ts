import { describe, expect, it } from "vitest";

import { summarizePolicy } from "./format";
import { readPolicyYaml } from "./yaml";

// The shapes GitLab returns for one policy: a document marker, wrapped plain and quoted
// scalars, sequences level with their key, and `[]`.
const PEER_APPROVAL = `---
name: Peer approval
description: Every merge request into a protected branch needs one approval from a
  Developer or above who neither opened it nor committed to it.
enabled: true
policy_scope:
  projects:
    excluding: []
actions:
- type: require_approval
  approvals_required: 1
  role_approvers:
  - developer
  - maintainer
  - owner
rules:
- type: any_merge_request
  branch_type: protected
  commits: any
approval_settings:
  prevent_approval_by_author: true
  prevent_approval_by_commit_author: true
  remove_approvals_with_new_commit: true
  block_branch_modification: true
  prevent_pushing_and_force_pushing: true
fallback_behavior:
  fail: closed
`;

const SCAN_POLICY = `---
name: Scan policy
description: ''
enabled: true
policy_scope:
  projects:
    excluding: []
rules:
- type: pipeline
  branch_type: default
- type: pipeline
  branch_type: target_default
  pipeline_sources:
    including:
    - merge_request_event
actions:
- scan: secret_detection
  template: latest
  variables:
    SECURE_ENABLE_LOCAL_CONFIGURATION: 'false'
- scan: sast
  template: latest
- scan: sast_iac
  template: latest
- scan: dependency_scanning
  template: latest
skip_ci:
  allowed: true
`;

const LICENSE_POLICY = `---
name: License Approval Policy
description: 'Requires approval when a dependency introduces a denied license, a strong-copyleft
  license, or one GitLab could not identify (DC BLOX''s own license is allowed) -- reviewed
  2026-08-09.'
enabled: true
actions:
- type: require_approval
  approvals_required: 1
  user_approvers:
  - alice
  - bob
rules:
- type: license_finding
  branches: []
  match_on_inclusion_license: true
  license_types:
  - GPL-3.0
  - AGPL-3.0
  - unknown
  license_states:
  - newly_detected
`;

describe("readPolicyYaml", () => {
  it("reads GitLab's block YAML, including wrapped scalars and level sequences", () => {
    expect(readPolicyYaml(PEER_APPROVAL)).toEqual({
      name: "Peer approval",
      description:
        "Every merge request into a protected branch needs one approval from a Developer or above who neither opened it nor committed to it.",
      enabled: true,
      policy_scope: { projects: { excluding: [] } },
      actions: [
        {
          type: "require_approval",
          approvals_required: 1,
          role_approvers: ["developer", "maintainer", "owner"],
        },
      ],
      rules: [{ type: "any_merge_request", branch_type: "protected", commits: "any" }],
      approval_settings: {
        prevent_approval_by_author: true,
        prevent_approval_by_commit_author: true,
        remove_approvals_with_new_commit: true,
        block_branch_modification: true,
        prevent_pushing_and_force_pushing: true,
      },
      fallback_behavior: { fail: "closed" },
    });
  });

  it("joins a wrapped single-quoted scalar and unescapes doubled quotes", () => {
    const parsed = readPolicyYaml(LICENSE_POLICY) as Record<string, unknown>;
    expect(parsed.description).toBe(
      "Requires approval when a dependency introduces a denied license, a strong-copyleft license, or one GitLab could not identify (DC BLOX's own license is allowed) -- reviewed 2026-08-09.",
    );
    expect(parsed.rules).toEqual([
      {
        type: "license_finding",
        branches: [],
        match_on_inclusion_license: true,
        license_types: ["GPL-3.0", "AGPL-3.0", "unknown"],
        license_states: ["newly_detected"],
      },
    ]);
  });

  it.each([
    ["an anchor", "a: &x 1\nb: *x\n"],
    ["a tag", "a: !!python/object 1\n"],
    ["a block scalar", "a: |\n  text\n"],
    ["a flow collection with content", "a: [1, 2]\n"],
    ["an unterminated quote", "a: 'open\n"],
    ["a duplicate key", "a: 1\na: 2\n"],
    ["tab indentation", "a:\n\tb: 1\n"],
    ["stray indentation", "a: 1\n    b: 2\nc:\n  d: 1\n e: 2\n"],
  ])("gives up on %s instead of guessing", (_, source) => {
    expect(readPolicyYaml(source)).toBeUndefined();
  });

  it("returns undefined for no YAML", () => {
    expect(readPolicyYaml(null)).toBeUndefined();
    expect(readPolicyYaml("")).toBeUndefined();
  });
});

describe("summarizePolicy", () => {
  it("summarises a scan execution policy and flags skip_ci", () => {
    expect(summarizePolicy("scan_execution", SCAN_POLICY)).toEqual({
      lines: [
        "Runs secret detection, SAST, SAST IaC and dependency scanning.",
        "When: in pipelines on the default branch; in merge request pipelines targeting the default branch.",
      ],
      notable: [
        {
          level: "warn",
          text: "skip_ci is allowed: anyone can skip this policy's jobs with [skip ci].",
        },
      ],
    });
  });

  it("summarises an approval policy without flags when it fails closed", () => {
    const summary = summarizePolicy("approval", PEER_APPROVAL);
    expect(summary?.lines).toEqual([
      "Requires 1 approval from developers, maintainers and owners.",
      "For any merge request into protected branches.",
      "Settings: authors can't approve their own merge requests; commit authors can't approve; new commits remove approvals; branch protection can't be changed; pushing and force-pushing are blocked.",
    ]);
    expect(summary?.notable).toEqual([]);
  });

  it("flags an approval policy that fails open or lets authors approve", () => {
    const risky = PEER_APPROVAL.replace("fail: closed", "fail: open").replace(
      "prevent_approval_by_author: true",
      "prevent_approval_by_author: false",
    );
    expect(summarizePolicy("approval", risky)?.notable.map((item) => item.text)).toEqual([
      "Authors may approve their own merge requests.",
      "Fails open: if GitLab can't evaluate the policy, merge requests are not blocked.",
    ]);
  });

  it("summarises a license approval rule", () => {
    expect(summarizePolicy("approval", LICENSE_POLICY)?.lines).toEqual([
      "Requires 1 approval from alice and bob.",
      "When a merge request adds one of these licenses (3 listed).",
    ]);
  });

  it("returns null for YAML it can't read, so the page shows the YAML alone", () => {
    expect(summarizePolicy("approval", "a: &x 1\n")).toBeNull();
  });
});
