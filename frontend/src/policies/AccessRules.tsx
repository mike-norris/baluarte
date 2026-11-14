import { useId, type ReactNode } from "react";

import {
  MAX_GRACE_HOURS,
  MAX_PRIVILEGED,
  MAX_PROJECT_APPROVALS,
  MAX_TOKEN_DAYS,
  MIN_GRACE_HOURS,
  MIN_PRIVILEGED,
  MIN_PROJECT_APPROVALS,
  MIN_TOKEN_DAYS,
  offMembersRule,
  offProjectApprovalsRule,
  offTokensRule,
  offTwoFactorRule,
  severities,
  severityLabels,
  type BaselineForm,
  type Severity,
} from "./yaml";

const ruleCard = "border rounded p-3 mb-3";

export function SeveritySelect({
  value,
  onChange,
}: {
  value: Severity;
  onChange: (severity: Severity) => void;
}) {
  return (
    <div className="max-w-xs">
      <label className="fw-medium">
        Severity
        <select
          value={value}
          onChange={(event) => {
            onChange(event.target.value as Severity);
          }}
          className="form-control mt-1"
        >
          {severities.map((severity) => (
            <option key={severity} value={severity}>
              {severityLabels[severity]}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

/** A whole-number field. An optional one is empty for null; a required one keeps NaN as empty. */
export function WholeNumber({
  label,
  value,
  min,
  max,
  optional = false,
  onChange,
}: {
  label: string;
  value: number | null;
  min: number;
  max: number;
  optional?: boolean;
  onChange: (value: number | null) => void;
}) {
  const id = useId();
  return (
    <label className="d-block max-w-xs fw-medium" htmlFor={id}>
      {label}
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        step={1}
        value={value === null || Number.isNaN(value) ? "" : value}
        onChange={(event) => {
          onChange(optional && event.target.value === "" ? null : event.target.valueAsNumber);
        }}
        className="form-control mt-1"
      />
    </label>
  );
}

export function Check({
  checked,
  onChange,
  children,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  children: ReactNode;
}) {
  return (
    <label className="d-flex align-items-center gap-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => {
          onChange(event.target.checked);
        }}
      />
      {children}
    </label>
  );
}

export function Card({
  label,
  title,
  onRemove,
  children,
}: {
  label: string;
  title: string;
  onRemove: () => void;
  children: ReactNode;
}) {
  return (
    <section aria-label={label} className={ruleCard}>
      <div className="d-flex flex-wrap align-items-center justify-content-between gap-2">
        <p className="fw-medium">{title}</p>
        <button type="button" onClick={onRemove} className="btn btn-sm">
          Remove rule
        </button>
      </div>
      <div className="pl-6">{children}</div>
    </section>
  );
}

export const help = "form-hint";

/**
 * The access-control and project merge request approval rules: off unless added, each with its
 * own card once added.
 */
export function AccessRules({
  form,
  onChange,
}: {
  form: BaselineForm;
  onChange: (form: BaselineForm) => void;
}) {
  const { projectApprovals, members, twoFactor, tokens } = form;
  const add: { label: string; on: () => void }[] = [];
  if (!projectApprovals.enabled) {
    add.push({
      label: "Add rule: project approval settings",
      on: () => {
        onChange({ ...form, projectApprovals: { ...offProjectApprovalsRule(), enabled: true } });
      },
    });
  }
  if (!members.enabled) {
    add.push({
      label: "Add rule: privileged members",
      on: () => {
        onChange({ ...form, members: { ...offMembersRule(), enabled: true } });
      },
    });
  }
  if (!twoFactor.enabled) {
    add.push({
      label: "Add rule: two-factor authentication",
      on: () => {
        onChange({ ...form, twoFactor: { ...offTwoFactorRule(), enabled: true } });
      },
    });
  }
  if (!tokens.enabled) {
    add.push({
      label: "Add rule: access token lifetime",
      on: () => {
        onChange({ ...form, tokens: { ...offTokensRule(), enabled: true } });
      },
    });
  }

  return (
    <>
      {projectApprovals.enabled && (
        <Card
          label="Project approval settings rule"
          title="Merge requests need approval under the project's approval rules"
          onRemove={() => {
            onChange({ ...form, projectApprovals: offProjectApprovalsRule() });
          }}
        >
          <WholeNumber
            label="Approvals required on the default branch, at least"
            value={projectApprovals.minApprovals}
            min={MIN_PROJECT_APPROVALS}
            max={MAX_PROJECT_APPROVALS}
            onChange={(value) => {
              onChange({
                ...form,
                projectApprovals: { ...projectApprovals, minApprovals: value ?? Number.NaN },
              });
            }}
          />
          <p className={help}>
            The largest number any approval rule covering the default branch requires. Use 0 to
            check only the settings below.
          </p>
          <Check
            checked={projectApprovals.forbidAuthorApproval}
            onChange={(checked) => {
              onChange({
                ...form,
                projectApprovals: { ...projectApprovals, forbidAuthorApproval: checked },
              });
            }}
          >
            Authors can&apos;t approve their own merge requests
          </Check>
          <Check
            checked={projectApprovals.forbidCommitterApproval}
            onChange={(checked) => {
              onChange({
                ...form,
                projectApprovals: { ...projectApprovals, forbidCommitterApproval: checked },
              });
            }}
          >
            Anyone who committed can&apos;t approve
          </Check>
          <Check
            checked={projectApprovals.resetApprovalsOnPush}
            onChange={(checked) => {
              onChange({
                ...form,
                projectApprovals: { ...projectApprovals, resetApprovalsOnPush: checked },
              });
            }}
          >
            A new push removes existing approvals
          </Check>
          <Check
            checked={projectApprovals.forbidOverride}
            onChange={(checked) => {
              onChange({
                ...form,
                projectApprovals: { ...projectApprovals, forbidOverride: checked },
              });
            }}
          >
            A merge request can&apos;t change its approval rules
          </Check>
          <p className={help}>
            Reads the project&apos;s own approval settings, which need GitLab Premium. A GitLab
            approval policy is checked by the required approvals rule instead.
          </p>
          <SeveritySelect
            value={projectApprovals.severity}
            onChange={(severity) => {
              onChange({ ...form, projectApprovals: { ...projectApprovals, severity } });
            }}
          />
        </Card>
      )}

      {members.enabled && (
        <Card
          label="Privileged members rule"
          title="Few members have Maintainer or Owner access"
          onRemove={() => {
            onChange({ ...form, members: offMembersRule() });
          }}
        >
          <WholeNumber
            label="Members with Maintainer or Owner access, at most"
            value={members.maxPrivileged}
            min={MIN_PRIVILEGED}
            max={MAX_PRIVILEGED}
            onChange={(value) => {
              onChange({ ...form, members: { ...members, maxPrivileged: value ?? Number.NaN } });
            }}
          />
          <p className={help}>
            Counts each person once, whether the access is given on the project or inherited from a
            group above it.
          </p>
          <Check
            checked={members.forbidInactiveAccounts}
            onChange={(checked) => {
              onChange({ ...form, members: { ...members, forbidInactiveAccounts: checked } });
            }}
          >
            No blocked or deactivated account keeps access
          </Check>
          <SeveritySelect
            value={members.severity}
            onChange={(severity) => {
              onChange({ ...form, members: { ...members, severity } });
            }}
          />
        </Card>
      )}

      {twoFactor.enabled && (
        <Card
          label="Two-factor authentication rule"
          title="A group above every project requires two-factor authentication"
          onRemove={() => {
            onChange({ ...form, twoFactor: offTwoFactorRule() });
          }}
        >
          <WholeNumber
            label="Grace period in hours, at most (optional)"
            value={twoFactor.maxGraceHours}
            min={MIN_GRACE_HOURS}
            max={MAX_GRACE_HOURS}
            optional
            onChange={(value) => {
              onChange({ ...form, twoFactor: { ...twoFactor, maxGraceHours: value } });
            }}
          />
          <p className={help}>
            Leave the grace period empty to accept any. When no group requires two-factor
            authentication, only an administrator token can read whether the whole instance does;
            without one the result is unknown, never failing or passing.
          </p>
          <SeveritySelect
            value={twoFactor.severity}
            onChange={(severity) => {
              onChange({ ...form, twoFactor: { ...twoFactor, severity } });
            }}
          />
        </Card>
      )}

      {tokens.enabled && (
        <Card
          label="Access token lifetime rule"
          title="Project access tokens expire"
          onRemove={() => {
            onChange({ ...form, tokens: offTokensRule() });
          }}
        >
          <WholeNumber
            label="Days from creation to expiry, at most"
            value={tokens.maxLifetimeDays}
            min={MIN_TOKEN_DAYS}
            max={MAX_TOKEN_DAYS}
            onChange={(value) => {
              onChange({ ...form, tokens: { ...tokens, maxLifetimeDays: value ?? Number.NaN } });
            }}
          />
          <WholeNumber
            label="Days a token may go unused, at most (optional)"
            value={tokens.maxUnusedDays}
            min={MIN_TOKEN_DAYS}
            max={MAX_TOKEN_DAYS}
            optional
            onChange={(value) => {
              onChange({ ...form, tokens: { ...tokens, maxUnusedDays: value } });
            }}
          />
          <p className={help}>
            Checks every active project access token. GitLab shows them only to a Maintainer, so
            with a less privileged token the result is unknown.
          </p>
          <SeveritySelect
            value={tokens.severity}
            onChange={(severity) => {
              onChange({ ...form, tokens: { ...tokens, severity } });
            }}
          />
        </Card>
      )}

      {add.length > 0 && (
        <div>
          <div className="d-flex flex-wrap gap-2">
            {add.map((button) => (
              <button key={button.label} type="button" onClick={button.on} className="btn">
                {button.label}
              </button>
            ))}
          </div>
          <p className={help}>
            Access control and the project&apos;s own merge request approval settings. Where
            Baluarte can&apos;t read a value, the result is unknown, never passing.
          </p>
        </div>
      )}
    </>
  );
}
