import { secondaryClass } from "../components/styles";
import { Card, Check, help, SeveritySelect, WholeNumber } from "./AccessRules";
import {
  MAX_APPROVALS,
  MAX_WINDOW_DAYS,
  MIN_APPROVALS,
  MIN_WINDOW_DAYS,
  offDeployedRule,
  offPipelineRule,
  offReviewedRule,
  type BaselineForm,
} from "./yaml";

function WindowDays({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  return (
    <WholeNumber
      label="Window in days"
      value={value}
      min={MIN_WINDOW_DAYS}
      max={MAX_WINDOW_DAYS}
      onChange={(next) => {
        onChange(next ?? Number.NaN);
      }}
    />
  );
}

/**
 * The change rules: what was merged or pushed to each default branch over a window of days,
 * off unless added, each with its own card once added.
 */
export function ChangeRules({
  form,
  onChange,
}: {
  form: BaselineForm;
  onChange: (form: BaselineForm) => void;
}) {
  const { reviewed, pipeline, deployed } = form;
  const add: { label: string; on: () => void }[] = [];
  if (!reviewed.enabled) {
    add.push({
      label: "Add rule: changes reviewed",
      on: () => {
        onChange({ ...form, reviewed: { ...offReviewedRule(), enabled: true } });
      },
    });
  }
  if (!pipeline.enabled) {
    add.push({
      label: "Add rule: change pipelines passed",
      on: () => {
        onChange({ ...form, pipeline: { ...offPipelineRule(), enabled: true } });
      },
    });
  }
  if (!deployed.enabled) {
    add.push({
      label: "Add rule: deployments reviewed",
      on: () => {
        onChange({ ...form, deployed: { ...offDeployedRule(), enabled: true } });
      },
    });
  }

  return (
    <>
      {reviewed.enabled && (
        <Card
          label="Changes reviewed rule"
          title="Every change was approved by someone other than its author"
          onRemove={() => {
            onChange({ ...form, reviewed: offReviewedRule() });
          }}
        >
          <WindowDays
            value={reviewed.windowDays}
            onChange={(windowDays) => {
              onChange({ ...form, reviewed: { ...reviewed, windowDays } });
            }}
          />
          <WholeNumber
            label="Approvals by people other than the author, at least"
            value={reviewed.minApprovals}
            min={MIN_APPROVALS}
            max={MAX_APPROVALS}
            onChange={(value) => {
              onChange({ ...form, reviewed: { ...reviewed, minApprovals: value ?? Number.NaN } });
            }}
          />
          <Check
            checked={reviewed.forbidSelfApproval}
            onChange={(checked) => {
              onChange({ ...form, reviewed: { ...reviewed, forbidSelfApproval: checked } });
            }}
          >
            A change its author approved fails
          </Check>
          <Check
            checked={reviewed.allowDirectPush}
            onChange={(checked) => {
              onChange({ ...form, reviewed: { ...reviewed, allowDirectPush: checked } });
            }}
          >
            Allow direct pushes to the default branch
          </Check>
          <p className={help}>
            With no changes in the window the rule passes. A change whose approvals can&apos;t be
            read makes the result unknown.
          </p>
          <SeveritySelect
            value={reviewed.severity}
            onChange={(severity) => {
              onChange({ ...form, reviewed: { ...reviewed, severity } });
            }}
          />
        </Card>
      )}

      {pipeline.enabled && (
        <Card
          label="Change pipelines rule"
          title="Every merged change passed its pipeline"
          onRemove={() => {
            onChange({ ...form, pipeline: offPipelineRule() });
          }}
        >
          <WindowDays
            value={pipeline.windowDays}
            onChange={(windowDays) => {
              onChange({ ...form, pipeline: { ...pipeline, windowDays } });
            }}
          />
          <Check
            checked={pipeline.requireTests}
            onChange={(checked) => {
              onChange({ ...form, pipeline: { ...pipeline, requireTests: checked } });
            }}
          >
            Require a test report with no failed tests
          </Check>
          <p className={help}>
            Judges each merge request by its head pipeline, and a direct push by its commit&apos;s
            pipeline if it has one.
          </p>
          <SeveritySelect
            value={pipeline.severity}
            onChange={(severity) => {
              onChange({ ...form, pipeline: { ...pipeline, severity } });
            }}
          />
        </Card>
      )}

      {deployed.enabled && (
        <Card
          label="Deployments reviewed rule"
          title="Every production deployment deployed a reviewed change"
          onRemove={() => {
            onChange({ ...form, deployed: offDeployedRule() });
          }}
        >
          <WindowDays
            value={deployed.windowDays}
            onChange={(windowDays) => {
              onChange({ ...form, deployed: { ...deployed, windowDays } });
            }}
          />
          <p className={help}>
            Only deployments to the production environments named in Settings are read.
          </p>
          <SeveritySelect
            value={deployed.severity}
            onChange={(severity) => {
              onChange({ ...form, deployed: { ...deployed, severity } });
            }}
          />
        </Card>
      )}

      {add.length > 0 && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {add.map((button) => (
              <button
                key={button.label}
                type="button"
                onClick={button.on}
                className={secondaryClass}
              >
                {button.label}
              </button>
            ))}
          </div>
          <p className={help}>
            Change evidence: the merge requests merged and commits pushed to each default branch.
            Where Baluarte can&apos;t read a change&apos;s review or pipeline, the result is
            unknown, never passing.
          </p>
        </div>
      )}
    </>
  );
}
