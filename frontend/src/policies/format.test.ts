import { describe, expect, it } from "vitest";

import type { PolicyChange } from "../api/policies";
import { approvedMessage } from "./format";

const approvedVersion: PolicyChange = {
  id: 5,
  kind: "version",
  state: "approved",
  policy_name: "acme-baseline",
  version: 2,
  yaml_digest: "a".repeat(64),
  base_version: 1,
  target_type: null,
  target_id: null,
  target_path: null,
  assignment_id: null,
  review_required: true,
  self_approved: false,
  migrated: false,
  proposed_by: { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" },
  proposed_at: "2026-11-01T10:00:00Z",
  decided_by: { actor: "user:2", user_id: 2, display_name: "Grace Hopper" },
  decided_at: "2026-11-02T10:00:00Z",
  decision_note: null,
};

describe("approvedMessage", () => {
  it("says a new version is not in force where the previous one is applied", () => {
    const message = approvedMessage(approvedVersion);

    expect(message).toBe(
      "Approved: New version of acme-baseline v2 (replaces v1). Places that use v1 keep it until v2 is applied: use Review and apply on the baseline's page.",
    );
    expect(message).not.toContain("in force now");
  });

  it("says a new baseline checks nothing until it is applied", () => {
    expect(approvedMessage({ ...approvedVersion, version: 1, base_version: null })).toBe(
      "Approved: New baseline acme-baseline v1. Nothing checks it until it is applied: use Apply on the baseline's page.",
    );
  });

  it("says an assignment is in force now", () => {
    expect(
      approvedMessage({
        ...approvedVersion,
        kind: "assign",
        version: 2,
        base_version: null,
        target_type: "instance",
        target_id: 0,
      }),
    ).toMatch(/^Approved: Apply acme-baseline v2 to .+\. It is in force now\.$/);
  });

  it("says a removed assignment is no longer checked", () => {
    expect(
      approvedMessage({
        ...approvedVersion,
        kind: "unassign",
        version: 1,
        base_version: null,
        target_type: "group",
        target_id: 10,
        target_path: "acme/platform",
      }),
    ).toMatch(/^Approved: Stop checking acme-baseline v1 on .+\. It is no longer checked there\.$/);
  });
});
