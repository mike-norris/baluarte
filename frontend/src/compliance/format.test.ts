import { describe, expect, it } from "vitest";

import { applyMinimums, defaultSettings, describeRequirement, isoDate, strictest } from "./format";

describe("compliance profile values", () => {
  it("takes the strictest requirement across the selected frameworks", () => {
    expect(strictest([], "session_idle_minutes")).toBeNull();
    expect(strictest(["soc2", "iso27001"], "session_idle_minutes")).toEqual({
      op: "<=",
      value: 30,
      frameworks: ["soc2", "iso27001"],
    });
    expect(strictest(["soc2", "nist_800_53_moderate"], "session_idle_minutes")).toEqual({
      op: "<=",
      value: 15,
      frameworks: ["nist_800_53_moderate"],
    });
    expect(strictest(["soc2"], "job_retention_days")).toBeNull();
  });

  it("moves only the values that miss a requirement, and never deployment values", () => {
    const loose = { ...defaultSettings, audit_retention_days: 730, session_idle_minutes: 10 };
    expect(applyMinimums(loose, ["soc2"])).toEqual({ ...loose, require_review: true });
    expect(
      applyMinimums(
        defaultSettings,
        ["nist_800_53_moderate"],
        (name) => name === "session_idle_minutes",
      ),
    ).toEqual({ ...defaultSettings, require_review: true });
  });

  it("describes requirements in plain words", () => {
    expect(describeRequirement("audit_retention_days", { op: ">=", value: 365 })).toBe(
      "At least 365 days",
    );
    expect(describeRequirement("session_idle_minutes", { op: "<=", value: 15 })).toBe(
      "At most 15 minutes",
    );
    expect(describeRequirement("require_review", { op: "==", value: true })).toBe("Required");
  });

  it("counts expiry dates in whole UTC days", () => {
    expect(isoDate(new Date("2026-11-02T23:30:00Z"), 1)).toBe("2026-11-03");
    expect(isoDate(new Date("2026-11-02T00:00:00Z"), 365)).toBe("2027-11-02");
  });
});
