import { describe, expect, it } from "vitest";

import { lineDiff } from "./diff";

describe("lineDiff", () => {
  it("marks removed and added lines around unchanged ones", () => {
    expect(lineDiff("a\nb\nc", "a\nx\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "added", text: "x" },
      { kind: "same", text: "c" },
    ]);
  });

  it("handles lines added at the end, and identical text", () => {
    expect(lineDiff("a", "a\nb")).toEqual([
      { kind: "same", text: "a" },
      { kind: "added", text: "b" },
    ]);
    expect(lineDiff("a\nb", "a\nb").every((line) => line.kind === "same")).toBe(true);
  });
});
