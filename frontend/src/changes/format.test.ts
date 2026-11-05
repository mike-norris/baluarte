import { describe, expect, it } from "vitest";

import {
  approvalsPart,
  changeName,
  deploymentPart,
  failingChanges,
  pipelinePart,
  readChangeFilters,
  writeChangeFilters,
} from "./format";

describe("change evidence words", () => {
  it("never reads missing evidence as known", () => {
    for (const part of [approvalsPart(undefined), pipelinePart(null), deploymentPart(undefined)]) {
      expect(part).toEqual({ text: "Unknown", known: false });
    }
    expect(approvalsPart({ status: "unknown", reason: "approvals are not readable" })).toEqual({
      text: "Unknown: approvals are not readable",
      known: false,
    });
  });

  it("describes approvals, including self-approval and approvals still needed", () => {
    expect(
      approvalsPart({
        status: "known",
        approved_by: [{ username: "dev-one", approved_at: null }],
        required: 2,
        left: 1,
        self_approved: true,
        committer_approved: null,
      }).text,
    ).toBe("1 approval (dev-one); 2 approvals required; 1 still needed; approved by its author");
  });

  it("counts errored tests as failed", () => {
    expect(
      pipelinePart({
        status: "known",
        id: 9,
        pipeline_status: "failed",
        source: "push",
        finished_at: null,
        tests: { total: 10, success: 7, failed: 1, skipped: 0, error: 2 },
      }).text,
    ).toBe("Pipeline 9 failed; 10 tests, 3 failed, 0 skipped");
  });

  it("says when nothing was deployed to a watched environment", () => {
    expect(deploymentPart({ status: "known", items: [] })).toEqual({
      text: "Not deployed to a watched environment",
      known: true,
    });
  });

  it("names merge requests by IID and pushes by short SHA", () => {
    expect(changeName({ kind: "merge_request", iid: 7, sha: "f".repeat(40), title: "Fix" })).toBe(
      "!7 Fix",
    );
    expect(
      changeName({ kind: "direct_push", iid: null, sha: "0123456789abcdef", title: null }),
    ).toBe("Direct push 01234567");
  });

  it("reads filters from the URL, dropping invalid values", () => {
    const filters = readChangeFilters(
      new URLSearchParams(
        "project=105&kind=direct_push&outcome=nope&from=2026-10-01T00:00:00Z&to=later",
      ),
    );
    expect(filters).toEqual({ project: 105, kind: "direct_push", from: "2026-10-01T00:00:00Z" });
    expect(writeChangeFilters(filters).toString()).toBe(
      "project=105&kind=direct_push&from=2026-10-01T00%3A00%3A00Z",
    );
  });

  it("reads failing changes defensively", () => {
    expect(failingChanges(null)).toEqual({ items: [], total: 0 });
    expect(
      failingChanges({
        failing: [{ id: 1, kind: "direct_push", sha: "a" }, { id: "x" }, null],
        failing_total: 0,
      }),
    ).toEqual({
      items: [{ id: 1, kind: "direct_push", sha: "a", iid: null, title: null }],
      total: 1,
    });
  });
});
