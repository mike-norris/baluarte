import { describe, expect, it } from "vitest";

import { applyBlocks } from "../api/labels";
import {
  emptyTaxonomy,
  hasDriftFilters,
  isHexColor,
  parseAliases,
  readDriftFilters,
  taxonomyProblems,
  taxonomyYaml,
  valuesText,
  writeDriftFilters,
} from "./format";

describe("label formatting", () => {
  it("writes the taxonomy form as YAML the API can read", () => {
    const yaml = taxonomyYaml({
      name: "acme-labels",
      groups: ["acme", "acme/*"],
      strict: true,
      labels: [
        { name: "priority::high", color: "#D9534F", description: 'Say "now"', aliases: ["P1"] },
        { name: "type::bug", color: "#1f75cb", description: null, aliases: [] },
      ],
    });
    expect(yaml).toBe(`apiVersion: baluarte.io/v1alpha1
kind: LabelTaxonomy
metadata:
  name: "acme-labels"
spec:
  scope:
    groups: ["acme", "acme/*"]
  placement: top_level_group
  strict: true
  labels:
    - name: "priority::high"
      color: "#d9534f"
      description: "Say \\"now\\""
      aliases: ["P1"]
    - name: "type::bug"
      color: "#1f75cb"
`);
    expect(taxonomyYaml(emptyTaxonomy())).toContain("labels: []");
  });

  it("catches mistakes before proposing", () => {
    expect(
      taxonomyProblems({
        name: "Acme Labels",
        groups: [],
        strict: false,
        labels: [
          { name: "priority::high", color: "red", description: null, aliases: ["P1"] },
          { name: "type::bug", color: "#1f75cb", description: null, aliases: ["p1"] },
          { name: "", color: "#1f75cb", description: null, aliases: [] },
        ],
      }),
    ).toEqual([
      expect.stringContaining("Name the taxonomy"),
      'Add at least one group pattern, or "*" for all.',
      'Label "priority::high" needs a colour like #1f75cb.',
      '"p1" is used by both "priority::high" and "type::bug".',
      "Every label needs a name.",
    ]);
    expect(taxonomyProblems({ ...emptyTaxonomy(), name: "acme" })).toEqual([
      "Add at least one label.",
    ]);
  });

  it("reads colours, aliases and values", () => {
    expect(isHexColor("#1F75cb")).toBe(true);
    expect(isHexColor("#1f75c")).toBe(false);
    expect(parseAliases("P1, prio-high\nP1,, ")).toEqual(["P1", "prio-high"]);
    expect(valuesText({ name: "Bug", color: "#ff0000", description: "" })).toEqual([
      "name: Bug",
      "colour: #ff0000",
      "description: (empty)",
    ]);
    expect(valuesText(null)).toEqual([]);
  });

  it("keeps drift filters in the URL, dropping unknown kinds", () => {
    const filters = readDriftFilters(new URLSearchParams("group=acme&kind=nonsense&label=%20P1%20"));
    expect(filters).toEqual({ group: "acme", label: "P1" });
    expect(hasDriftFilters(filters)).toBe(true);
    const base = new URLSearchParams("as_of=2026-10-01T00:00:00Z&kind=color");
    expect(writeDriftFilters({ group: "acme" }, base).toString()).toBe(
      "as_of=2026-10-01T00%3A00%3A00Z&group=acme",
    );
  });

  it("explains why a plan can't be applied", () => {
    expect(applyBlocks(["label_writes_disabled", "write_token_missing", "something_new"])).toEqual([
      expect.stringContaining("BALUARTE_LABEL_WRITES"),
      expect.stringContaining("No label write token is set"),
      "something new.",
    ]);
  });
});
