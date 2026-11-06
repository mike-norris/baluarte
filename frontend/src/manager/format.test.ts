import { describe, expect, it } from "vitest";

import { percent, rowName } from "../api/manager";
import { parseTagParam, tagProblems, tagText } from "../api/tags";
import { againstEstimate, daysAgo, formatDuration, parseDate, periodProblem } from "../api/time";
import { analyst } from "../test/api";
import { auditor, manager } from "../test/manager";
import {
  canReadPeople,
  canReadTime,
  canWriteTags,
  currentOnlyText,
  focusText,
  managerTabs,
  parentPath,
  readFocus,
  writeFocus,
} from "./format";

describe("manager focus", () => {
  it("reads a group path, else a tag, from the URL", () => {
    expect(readFocus(new URLSearchParams("group=acme/payments"))).toEqual({
      group: "acme/payments",
    });
    expect(readFocus(new URLSearchParams("tag=team:payments"))).toEqual({ tag: "team:payments" });
    expect(readFocus(new URLSearchParams("group=acme&tag=team:payments"))).toEqual({
      group: "acme",
    });
    expect(readFocus(new URLSearchParams("group=../etc&tag=Team:x"))).toEqual({});
  });

  it("writes the focus and keeps other parameters", () => {
    const params = new URLSearchParams("bucket=month&group=old");
    expect(writeFocus({ tag: "team:payments" }, params).toString()).toBe(
      "bucket=month&tag=team%3Apayments",
    );
    expect(writeFocus({}, params).toString()).toBe("bucket=month");
  });

  it("describes the focus and finds a group's parent", () => {
    expect(focusText({})).toBe("all top-level groups");
    expect(focusText({ group: "acme/web" })).toBe("group acme/web");
    expect(focusText({ tag: "team:payments" })).toBe("tag team=payments");
    expect(parentPath("acme/web/api")).toBe("acme/web");
    expect(parentPath("acme")).toBeNull();
  });

  it("names the figures shown as of now", () => {
    expect(currentOnlyText([])).toBeNull();
    expect(currentOnlyText(["time", "tags"])).toBe(
      "Shown as of now, because Baluarte keeps no history of them: time tracking, tags.",
    );
  });
});

describe("manager permissions", () => {
  it("shows each section only to the roles that may read it", () => {
    const tabs = (user: typeof manager) =>
      managerTabs.filter((tab) => tab.visible(user)).map((tab) => tab.label);
    expect(tabs(manager)).toEqual(["Overview", "Trends", "Time", "Tags"]);
    expect(tabs(auditor)).toEqual(["Overview", "Trends", "Time", "Tags"]);
    expect(tabs(analyst)).toEqual(["Tags"]);
    expect([canReadPeople(manager), canReadPeople(auditor)]).toEqual([true, false]);
    expect([canWriteTags(manager), canWriteTags(auditor)]).toEqual([true, false]);
    expect(canReadTime(analyst)).toBe(false);
  });
});

describe("tags", () => {
  it("reads key:value and refuses anything else", () => {
    expect(parseTagParam("team:payments")).toEqual({ key: "team", value: "payments" });
    expect(parseTagParam("cost-center:cc-104")).toEqual({ key: "cost-center", value: "cc-104" });
    expect(parseTagParam("Team:payments")).toBeNull();
    expect(parseTagParam("team:a:b")).toBeNull();
    expect(parseTagParam("team")).toBeNull();
    expect(tagText({ key: "team", value: "payments" })).toBe("team=payments");
  });

  it("checks a new tag before sending it", () => {
    expect(tagProblems({ key: "team", value: "payments", color: "#1f6feb" })).toEqual({});
    expect(Object.keys(tagProblems({ key: "Team", value: "", color: "blue" }))).toEqual([
      "key",
      "value",
      "color",
    ]);
    expect(tagProblems({ key: "team", value: "x", description: "d".repeat(501) })).toHaveProperty(
      "description",
    );
  });
});

describe("time", () => {
  it("formats durations and estimates", () => {
    expect(formatDuration(0)).toBe("0m");
    expect(formatDuration(1_800)).toBe("30m");
    expect(formatDuration(36_000)).toBe("10h");
    expect(formatDuration(45_000)).toBe("12h 30m");
    expect(formatDuration(null)).toBe("No estimate");
    expect(againstEstimate(45_000, 36_000)).toBe("125% of estimate");
    expect(againstEstimate(10, null)).toBe("No estimate");
    expect(againstEstimate(10, 0)).toBe("No estimate");
  });

  it("checks periods", () => {
    expect(daysAgo(30, new Date("2026-11-06T12:00:00Z"))).toBe("2026-10-07");
    expect(parseDate("2026-11-06")).toBe("2026-11-06");
    expect(parseDate("06/11/2026")).toBeNull();
    expect(periodProblem("2026-11-06", "2026-11-01")).toBe(
      "The start must be on or before the end.",
    );
    expect(periodProblem("", "2026-11-01")).toBe("Enter both dates.");
    expect(periodProblem("2026-11-01", "2026-11-06")).toBeNull();
  });
});

describe("overview figures", () => {
  it("names rows and shows shares", () => {
    expect(rowName({ type: "group", gitlab_id: 1, path: "acme" })).toBe("acme");
    expect(rowName({ type: "tag", key: "team", value: "web" })).toBe("team=web");
    expect(percent(0.756)).toBe("76%");
    expect(percent(null)).toBe("–");
  });
});
