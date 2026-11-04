import { describe, expect, it } from "vitest";

import { controlPath, refusalText } from "../api/controls";
import { mappingChange } from "../test/controls";
import {
  isOwnChange,
  mappingChangeText,
  mappingDecisionText,
  validateChange,
  type ChangeForm,
} from "./format";

const today = new Date("2026-11-03T12:00:00Z");
const reason = "The organization reviews this outside GitLab every quarter.";

function form(extra: Partial<ChangeForm>): ChangeForm {
  return {
    kind: "map",
    ruleType: "",
    justification: reason,
    evidenceRef: "",
    reviewBy: "",
    supersedesId: null,
    ...extra,
  };
}

describe("mapping change checks", () => {
  it("builds a map request with only the fields it needs", () => {
    expect(
      validateChange(
        "soc2",
        "CC8.1",
        form({ ruleType: "gitlab.project.merge_request_approvals", reviewBy: "2027-01-01" }),
        today,
      ),
    ).toEqual({
      request: {
        framework: "soc2",
        control_id: "CC8.1",
        kind: "map",
        rule_type: "gitlab.project.merge_request_approvals",
        justification: reason,
      },
    });
  });

  it("requires a justification of 20 to 2000 characters", () => {
    expect(
      validateChange("soc2", "CC8.1", form({ ruleType: "x", justification: "  too short  " })),
    ).toEqual({
      errors: ["Justification: explain the reason in at least 20 characters."],
    });
    expect(
      validateChange("soc2", "CC8.1", form({ ruleType: "x", justification: "a".repeat(2001) })),
    ).toEqual({ errors: ["Justification: at most 2000 characters."] });
  });

  it("requires a rule type to map or unmap, and the change to withdraw", () => {
    expect(validateChange("soc2", "CC8.1", form({ kind: "unmap" }))).toEqual({
      errors: ["Choose a rule type."],
    });
    expect(validateChange("soc2", "CC8.1", form({ kind: "withdraw" }))).toEqual({
      errors: ["Choose the change to withdraw."],
    });
    expect(
      validateChange("soc2", "CC8.1", form({ kind: "withdraw", supersedesId: 4 }), today),
    ).toEqual({
      request: {
        framework: "soc2",
        control_id: "CC8.1",
        kind: "withdraw",
        supersedes_id: 4,
        justification: reason,
      },
    });
  });

  it("requires a review date within 365 days for not applicable and manual", () => {
    for (const kind of ["not_applicable", "manual"] as const) {
      const dated = (extra: Partial<ChangeForm>) =>
        form({ evidenceRef: "Access review records", ...extra });
      expect(validateChange("soc2", "CC6.8", dated({ kind }), today)).toEqual({
        errors: ["Review by: choose the date this decision must be reviewed again."],
      });
      expect(validateChange("soc2", "CC6.8", dated({ kind, reviewBy: "2026-11-03" }), today)).toEqual(
        { errors: ["Review by: choose a date after today."] },
      );
      expect(validateChange("soc2", "CC6.8", dated({ kind, reviewBy: "2027-11-04" }), today)).toEqual(
        { errors: ["Review by: at most 365 days ahead."] },
      );
    }
    expect(
      validateChange(
        "soc2",
        "CC6.8",
        form({ kind: "manual", reviewBy: "2027-11-03", evidenceRef: " Access review records " }),
        today,
      ),
    ).toEqual({
      request: {
        framework: "soc2",
        control_id: "CC6.8",
        kind: "manual",
        justification: reason,
        evidence_ref: "Access review records",
        review_by: "2027-11-03",
      },
    });
  });

  it("requires where the evidence is for a control met outside GitLab", () => {
    expect(
      validateChange("soc2", "CC6.8", form({ kind: "manual", reviewBy: "2027-11-03" }), today),
    ).toEqual({
      errors: ["Where the evidence is: say where the evidence that the control is met is kept."],
    });
  });

  it("limits where the evidence is to 500 characters", () => {
    expect(
      validateChange("soc2", "CC8.1", form({ ruleType: "x", evidenceRef: "e".repeat(501) })),
    ).toEqual({ errors: ["Where the evidence is: at most 500 characters."] });
  });
});

describe("mapping change words", () => {
  it("describes each kind of change", () => {
    expect(mappingChangeText(mappingChange(1))).toBe(
      "Count Project approval settings toward SOC 2 CC8.1",
    );
    expect(mappingChangeText(mappingChange(2, { kind: "unmap" }))).toBe(
      "Stop counting Project approval settings toward SOC 2 CC8.1",
    );
    expect(
      mappingChangeText(
        mappingChange(3, {
          kind: "not_applicable",
          framework: "iso27001",
          control_id: "A.8.9",
          rule_type: null,
        }),
      ),
    ).toBe("Mark ISO 27001 A.8.9 not applicable");
    expect(mappingChangeText(mappingChange(4, { kind: "withdraw", supersedes_id: 2 }))).toBe(
      "Withdraw change 2 to SOC 2 CC8.1",
    );
  });

  it("names how a change was decided", () => {
    expect(mappingDecisionText(mappingChange(1))).toBe(
      "Waiting for someone other than the proposer",
    );
    expect(mappingDecisionText(mappingChange(1, { state: "active", self_approved: true }))).toBe(
      "Not reviewed: review was off, so it applied at once",
    );
    expect(
      mappingDecisionText(
        mappingChange(1, {
          state: "rejected",
          decided_by: { actor: "user:2", user_id: 2, display_name: "Grace Hopper" },
        }),
      ),
    ).toBe("Rejected by Grace Hopper");
    expect(mappingDecisionText(mappingChange(1, { state: "active", decided_by: 7 }))).toBe(
      "Approved by user 7",
    );
  });

  it("recognises the proposer in either shape", () => {
    expect(isOwnChange(mappingChange(1), 1)).toBe(true);
    expect(isOwnChange(mappingChange(1, { proposed_by: 2 }), 2)).toBe(true);
    expect(isOwnChange(mappingChange(1), 2)).toBe(false);
    expect(isOwnChange(mappingChange(1), undefined)).toBe(false);
  });

  it("encodes control IDs in paths", () => {
    expect(controlPath("nist_800_53_moderate", "AC-6(5)")).toBe(
      "/controls/nist_800_53_moderate/AC-6(5)",
    );
    expect(controlPath("soc2", "CC 8/1")).toBe("/controls/soc2/CC%208%2F1");
  });
});

describe("refusal reasons", () => {
  it("turns the API's reason codes into sentences", () => {
    expect(refusalText("already_mapped")).toBe(
      "that rule type already counts toward this control",
    );
    expect(refusalText({ code: "not_default" })).toBe(
      "only a pair from the built-in mapping can be removed",
    );
    expect(refusalText({ reasons: ["duplicate", "review_by_too_far"] })).toBe(
      "the same change is already in force or waiting for review; the review date must be at most 365 days ahead",
    );
    expect(refusalText({ reasons: [{ code: "some_new_reason" }] })).toBe("some new reason");
    expect(refusalText(["status_exists"])).toBe(
      "the control is already marked not applicable or met outside GitLab",
    );
    expect(refusalText({ message: "The control changed." })).toBe("The control changed.");
    expect(refusalText("A plain sentence.")).toBe("A plain sentence.");
    expect(refusalText(null)).toBeNull();
  });
});
