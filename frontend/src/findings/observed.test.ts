import { describe, expect, it } from "vitest";

import { observedSummary } from "./observed";

describe("observed value summaries", () => {
  it("summarises project approval settings and rules", () => {
    expect(
      observedSummary("gitlab.project.merge_request_approvals", {
        settings: {
          merge_requests_author_approval: false,
          merge_requests_disable_committers_approval: false,
          reset_approvals_on_push: true,
          disable_overriding_approvers_per_merge_request: false,
        },
        rules: [
          { name: "All members", rule_type: "any_approver", approvals_required: 1 },
          { name: "Security", rule_type: "regular", approvals_required: 2 },
        ],
      }),
    ).toBe(
      "2 approval rules, requiring at most 2 approvals; authors can't approve; committers can approve; a push removes approvals; merge requests can change their rules.",
    );
    expect(
      observedSummary("gitlab.project.merge_request_approvals", {
        merge_requests_author_approval: true,
        rules: [],
      }),
    ).toBe("No approval rules; authors can approve their own.");
    expect(observedSummary("gitlab.project.merge_request_approvals", { other: 1 })).toBeNull();
  });

  it("summarises privileged and inactive members", () => {
    expect(
      observedSummary("gitlab.project.privileged_members", {
        counts: { "40": 1, "50": 1 },
        members: [
          { username: "auditor", access_level: 50, state: "active", expires_at: null },
          { username: "acme-ops", access_level: 40, state: "blocked", expires_at: null },
          { username: "dev", access_level: 30, state: "deactivated", expires_at: null },
        ],
      }),
    ).toBe(
      "2 members with Maintainer or Owner access (acme-ops, auditor); 2 accounts not active: acme-ops (blocked), dev (deactivated).",
    );
    expect(
      observedSummary("gitlab.project.privileged_members", { privileged_count: 0, members: [] }),
    ).toBe("0 members with Maintainer or Owner access.");
    expect(observedSummary("gitlab.project.privileged_members", { counts: {} })).toBeNull();
  });

  it("summarises project access tokens, as a list or under tokens", () => {
    const list = [
      {
        id: 1,
        name: "deploy",
        active: true,
        revoked: false,
        created_at: "2026-01-01T00:00:00Z",
        expires_at: "2026-12-31",
        last_used_at: null,
      },
      {
        id: 2,
        name: "ci",
        active: true,
        revoked: false,
        created_at: "2026-10-01T00:00:00Z",
        expires_at: null,
        last_used_at: "2026-10-30T00:00:00Z",
      },
      { id: 3, name: "old", active: false, revoked: true },
    ];
    const expected =
      "2 active tokens; 1 without an expiry date; longest lifetime 364 days; 1 never used.";
    expect(observedSummary("gitlab.project.access_tokens", list)).toBe(expected);
    expect(observedSummary("gitlab.project.access_tokens", { tokens: list })).toBe(expected);
    expect(observedSummary("gitlab.project.access_tokens", [])).toBe(
      "No active project access tokens.",
    );
    expect(observedSummary("gitlab.project.access_tokens", "x")).toBeNull();
  });

  it("summarises two-factor enforcement above the project", () => {
    const groups = [
      { path: "acme", require_two_factor_authentication: true, two_factor_grace_period: 48 },
      {
        path: "acme/platform",
        require_two_factor_authentication: false,
        two_factor_grace_period: 48,
      },
    ];
    expect(
      observedSummary("gitlab.group.two_factor_required", { groups, enforced_by: "acme" }),
    ).toBe("Required by acme, grace period 48 hours.");
    expect(
      observedSummary("gitlab.group.two_factor_required", {
        groups: groups.slice(1),
        enforced_by: null,
        instance: { require_two_factor_authentication: false },
      }),
    ).toBe(
      "No group above the project requires two-factor authentication (1 group checked). Nor does the instance.",
    );
    expect(observedSummary("gitlab.group.two_factor_required", {})).toBeNull();
  });

  it("leaves other rule types to the generic rendering", () => {
    expect(observedSummary("gitlab.project.visibility", "private")).toBeNull();
    expect(observedSummary("gitlab.branch.default_protected", { protected: true })).toBeNull();
  });
});
