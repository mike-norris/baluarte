import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { ChangeRules } from "./ChangeRules";
import {
  emptyBaseline,
  formProblems,
  fromDocument,
  readableRules,
  recommendedBaseline,
  toDocument,
  toYaml,
  type BaselineForm,
} from "./yaml";

function withChangeRules(): BaselineForm {
  const form = recommendedBaseline();
  form.reviewed = { ...form.reviewed, enabled: true, minApprovals: 2 };
  form.pipeline = { ...form.pipeline, enabled: true, windowDays: 30, requireTests: true };
  form.deployed = { ...form.deployed, enabled: true };
  return form;
}

describe("change rules in the baseline form", () => {
  it("leaves them out of the recommended and blank baselines", () => {
    expect(toYaml(recommendedBaseline())).not.toContain("gitlab.change");
    expect(emptyBaseline().reviewed.enabled).toBe(false);
  });

  it("writes them in the contract's shape", () => {
    expect(toYaml(withChangeRules())).toContain(`    - id: changes-reviewed
      type: gitlab.change.reviewed
      window_days: 90
      min_approvals: 2
      forbid_self_approval: true
      allow_direct_push: false
      severity: high
    - id: changes-pipeline-passed
      type: gitlab.change.pipeline_passed
      window_days: 30
      require_tests: true
      severity: medium
    - id: deployments-reviewed
      type: gitlab.change.deployed_after_review
      window_days: 90
      severity: high
`);
  });

  it("round-trips through the form", () => {
    const form = withChangeRules();
    form.reviewed.allowDirectPush = true;
    form.deployed.description = "Only reviewed code reaches production.";
    const back = fromDocument(toDocument(form));
    expect(back?.reviewed).toEqual(form.reviewed);
    expect(back?.pipeline).toEqual(form.pipeline);
    expect(back?.deployed).toEqual(form.deployed);
  });

  it("applies the contract's defaults when optional fields are left out", () => {
    const document = toDocument(recommendedBaseline());
    (document.spec as { rules: unknown[] }).rules.push(
      { id: "reviewed", type: "gitlab.change.reviewed" },
      { id: "pipelines", type: "gitlab.change.pipeline_passed" },
      { id: "deployments", type: "gitlab.change.deployed_after_review" },
    );
    const form = fromDocument(document);
    expect(form?.reviewed).toMatchObject({
      enabled: true,
      windowDays: 90,
      minApprovals: 1,
      forbidSelfApproval: true,
      allowDirectPush: false,
    });
    expect(form?.pipeline).toMatchObject({ enabled: true, windowDays: 90, requireTests: false });
    expect(form?.deployed).toMatchObject({ enabled: true, windowDays: 90 });
  });

  it("opens as YAML when the form can't show the rule exactly", () => {
    const document = toDocument(withChangeRules());
    (document.spec as { rules: unknown[] }).rules.push({
      id: "again",
      type: "gitlab.change.reviewed",
    });
    expect(fromDocument(document)).toBeNull();
    const odd = toDocument(recommendedBaseline());
    (odd.spec as { rules: unknown[] }).rules.push({
      id: "odd",
      type: "gitlab.change.pipeline_passed",
      require_tests: "yes",
    });
    expect(fromDocument(odd)).toBeNull();
  });

  it("checks the limits before saving", () => {
    const form = withChangeRules();
    form.reviewed.windowDays = 366;
    form.reviewed.minApprovals = 0;
    form.pipeline.windowDays = Number.NaN;
    form.deployed.windowDays = 0;
    expect(formProblems(form)).toEqual([
      "Reviewed changes window: enter a whole number from 1 to 365.",
      "Reviewed changes approvals: enter a whole number from 1 to 20.",
      "Change pipelines window: enter a whole number from 1 to 365.",
      "Reviewed deployments window: enter a whole number from 1 to 365.",
    ]);
    const only = emptyBaseline();
    only.name = "changes-only";
    only.deployed.enabled = true;
    expect(formProblems(only)).toEqual([]);
  });

  it("describes them as sentences", () => {
    const rules = readableRules(toDocument(withChangeRules())).slice(2);
    expect(rules).toEqual([
      {
        id: "changes-reviewed",
        sentence:
          "Every change to a default branch over the last 90 days was approved by at least 2 people other than its author",
        detail:
          "The author's own approval doesn't count. A direct push to the default branch fails.",
        severity: "high",
      },
      {
        id: "changes-pipeline-passed",
        sentence:
          "Every change merged to a default branch over the last 30 days passed its pipeline",
        detail: "Its pipeline must have a test report with no failures.",
        severity: "medium",
      },
      {
        id: "deployments-reviewed",
        sentence: "Every production deployment over the last 90 days deployed a reviewed change",
        detail: null,
        severity: "high",
      },
    ]);
  });
});

function Editor({ seen }: { seen: (form: BaselineForm) => void }) {
  const [form, setForm] = useState(emptyBaseline);
  return (
    <ChangeRules
      form={form}
      onChange={(next) => {
        seen(next);
        setForm(next);
      }}
    />
  );
}

describe("change rule cards", () => {
  it("adds, edits and removes a change rule", async () => {
    const user = userEvent.setup();
    let latest = emptyBaseline();
    render(
      <Editor
        seen={(form) => {
          latest = form;
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add rule: changes reviewed" }));
    const card = screen.getByRole("region", { name: "Changes reviewed rule" });
    const window = within(card).getByLabelText("Window in days");
    await user.clear(window);
    await user.type(window, "30");
    await user.click(within(card).getByLabelText("Allow direct pushes to the default branch"));
    expect(latest.reviewed).toMatchObject({ enabled: true, windowDays: 30, allowDirectPush: true });
    expect(
      screen.queryByRole("button", { name: "Add rule: changes reviewed" }),
    ).not.toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Remove rule" }));
    expect(latest.reviewed.enabled).toBe(false);
    expect(screen.getByRole("button", { name: "Add rule: changes reviewed" })).toBeInTheDocument();
  });
});
