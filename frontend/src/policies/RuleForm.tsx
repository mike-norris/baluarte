import { useId } from "react";

import { fieldClass, secondaryClass } from "../components/styles";
import {
  MAX_APPROVALS,
  MIN_APPROVALS,
  offApprovalRule,
  offScanRule,
  scanLabels,
  scans,
  severities,
  severityLabels,
  visibilities,
  visibilityLabels,
  type BaselineForm,
  type Severity,
} from "./yaml";

function SeveritySelect({
  label,
  value,
  disabled,
  onChange,
}: {
  label: string;
  value: Severity;
  disabled: boolean;
  onChange: (severity: Severity) => void;
}) {
  return (
    <label className="text-sm font-medium">
      {label}
      <select
        value={value}
        disabled={disabled}
        onChange={(event) => {
          onChange(event.target.value as Severity);
        }}
        className={fieldClass}
      >
        {severities.map((severity) => (
          <option key={severity} value={severity}>
            {severityLabels[severity]}
          </option>
        ))}
      </select>
    </label>
  );
}

const ruleCard = "space-y-3 rounded border border-slate-200 p-3";

/**
 * The fields of a baseline: its name, which groups it covers, and its rules as switches. The
 * name is fixed when editing an existing baseline, because a new name makes a new baseline.
 */
export function RuleForm({
  form,
  onChange,
  nameLocked,
}: {
  form: BaselineForm;
  onChange: (form: BaselineForm) => void;
  nameLocked: boolean;
}) {
  const id = useId();
  const { visibility, branch, scan, approval } = form;

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-sm font-medium" htmlFor={`${id}-name`}>
          Name
          <input
            id={`${id}-name`}
            value={form.name}
            readOnly={nameLocked}
            maxLength={63}
            aria-describedby={`${id}-name-help`}
            onChange={(event) => {
              onChange({ ...form, name: event.target.value });
            }}
            className={`${fieldClass} ${nameLocked ? "bg-slate-100" : ""}`}
          />
        </label>
        <label className="text-sm font-medium" htmlFor={`${id}-description`}>
          Description (optional)
          <input
            id={`${id}-description`}
            value={form.description}
            maxLength={1000}
            onChange={(event) => {
              onChange({ ...form, description: event.target.value });
            }}
            className={fieldClass}
          />
        </label>
      </div>
      <p id={`${id}-name-help`} className="-mt-3 text-sm text-slate-600">
        {nameLocked
          ? "Saving adds the next version of this baseline. Earlier versions stay as they were."
          : "Lowercase letters, digits and hyphens, for example recommended-baseline. Findings name it."}
      </p>

      <fieldset className="space-y-3">
        <legend className="text-base font-semibold">Rules</legend>

        <div className={ruleCard}>
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={visibility.enabled}
              onChange={(event) => {
                onChange({ ...form, visibility: { ...visibility, enabled: event.target.checked } });
              }}
            />
            Projects have an allowed visibility
          </label>
          <fieldset disabled={!visibility.enabled} className="space-y-3 pl-6 disabled:opacity-60">
            <legend className="sr-only">Project visibility settings</legend>
            <div>
              <p className="text-sm font-medium" id={`${id}-allowed`}>
                Allowed visibility
              </p>
              <div
                className="mt-1 flex flex-wrap gap-4"
                role="group"
                aria-labelledby={`${id}-allowed`}
              >
                {visibilities.map((value) => (
                  <label key={value} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={visibility.allowed.includes(value)}
                      onChange={(event) => {
                        const allowed = event.target.checked
                          ? [...visibility.allowed, value]
                          : visibility.allowed.filter((item) => item !== value);
                        onChange({ ...form, visibility: { ...visibility, allowed } });
                      }}
                    />
                    {visibilityLabels[value]}
                  </label>
                ))}
              </div>
              <p className="mt-1 text-sm text-slate-600">
                A project with any other visibility fails. Public projects can be read by anyone who
                can reach GitLab.
              </p>
            </div>
            <div className="max-w-xs">
              <SeveritySelect
                label="Severity"
                value={visibility.severity}
                disabled={!visibility.enabled}
                onChange={(severity) => {
                  onChange({ ...form, visibility: { ...visibility, severity } });
                }}
              />
            </div>
          </fieldset>
        </div>

        <div className={ruleCard}>
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={branch.enabled}
              onChange={(event) => {
                onChange({ ...form, branch: { ...branch, enabled: event.target.checked } });
              }}
            />
            Every default branch is protected
          </label>
          <fieldset disabled={!branch.enabled} className="space-y-3 pl-6 disabled:opacity-60">
            <legend className="sr-only">Default branch protection settings</legend>
            <p className="text-sm text-slate-600">
              Passes when a protected-branch rule in GitLab, exact or wildcard, covers the
              project&apos;s default branch. An empty repository has no default branch, so its
              result is unknown.
            </p>
            <div className="max-w-xs">
              <SeveritySelect
                label="Severity"
                value={branch.severity}
                disabled={!branch.enabled}
                onChange={(severity) => {
                  onChange({ ...form, branch: { ...branch, severity } });
                }}
              />
            </div>
          </fieldset>
        </div>

        {scan.enabled && (
          <section aria-label="Required scans rule" className={ruleCard}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm font-medium">
                Projects run scans through a GitLab scan execution policy
              </p>
              <button
                type="button"
                onClick={() => {
                  onChange({ ...form, scan: offScanRule() });
                }}
                className={`${secondaryClass} text-xs`}
              >
                Remove rule
              </button>
            </div>
            <div className="space-y-3 pl-6">
              <div>
                <p className="text-sm font-medium" id={`${id}-scans`}>
                  Required scans
                </p>
                <div
                  className="mt-1 grid gap-2 sm:grid-cols-2"
                  role="group"
                  aria-labelledby={`${id}-scans`}
                >
                  {scans.map((value) => (
                    <label key={value} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={scan.scans.includes(value)}
                        onChange={(event) => {
                          const chosen = event.target.checked
                            ? [...scan.scans, value]
                            : scan.scans.filter((item) => item !== value);
                          onChange({
                            ...form,
                            scan: { ...scan, scans: chosen },
                          });
                        }}
                      />
                      {scanLabels[value]}
                    </label>
                  ))}
                </div>
                <p className="mt-1 text-sm text-slate-600">
                  Passes when the enabled scan execution policies reaching a project, together, run
                  every chosen scan on its default branch, in pipelines or on a schedule. Policies
                  that run only for merge requests don&apos;t count. GitLab enforces the policy;
                  Baluarte only reads it.
                </p>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={scan.allowSkipCi}
                  onChange={(event) => {
                    onChange({
                      ...form,
                      scan: { ...scan, allowSkipCi: event.target.checked },
                    });
                  }}
                />
                Count a policy that lets pipelines skip CI
              </label>
              <p className="-mt-2 pl-6 text-sm text-slate-600">
                As in GitLab, a policy without a <code>skip_ci</code> setting can be skipped. When
                this is off, only policies that forbid skipping count.
              </p>
              <div className="max-w-xs">
                <SeveritySelect
                  label="Severity"
                  value={scan.severity}
                  disabled={false}
                  onChange={(severity) => {
                    onChange({ ...form, scan: { ...scan, severity } });
                  }}
                />
              </div>
            </div>
          </section>
        )}

        {approval.enabled && (
          <section aria-label="Required approvals rule" className={ruleCard}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm font-medium">
                Merge requests need approval through a GitLab approval policy
              </p>
              <button
                type="button"
                onClick={() => {
                  onChange({ ...form, approval: offApprovalRule() });
                }}
                className={`${secondaryClass} text-xs`}
              >
                Remove rule
              </button>
            </div>
            <div className="space-y-3 pl-6">
              <label className="block max-w-xs text-sm font-medium" htmlFor={`${id}-approvals`}>
                Approvals required, at least
                <input
                  id={`${id}-approvals`}
                  type="number"
                  min={MIN_APPROVALS}
                  max={MAX_APPROVALS}
                  step={1}
                  value={Number.isNaN(approval.minApprovals) ? "" : approval.minApprovals}
                  onChange={(event) => {
                    onChange({
                      ...form,
                      approval: {
                        ...approval,
                        minApprovals: event.target.valueAsNumber,
                      },
                    });
                  }}
                  className={fieldClass}
                />
              </label>
              <p className="text-sm text-slate-600">
                Passes when one enabled approval policy reaching a project requires at least this
                many approvals on every merge request to a protected branch. Rules that ask for
                approval only when a scan or license finding matches don&apos;t count, so a license
                approval policy alone doesn&apos;t satisfy this.
              </p>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={approval.forbidAuthorApproval}
                  onChange={(event) => {
                    onChange({
                      ...form,
                      approval: {
                        ...approval,
                        forbidAuthorApproval: event.target.checked,
                      },
                    });
                  }}
                />
                The policy must stop the author approving
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={approval.forbidCommitterApproval}
                  onChange={(event) => {
                    onChange({
                      ...form,
                      approval: {
                        ...approval,
                        forbidCommitterApproval: event.target.checked,
                      },
                    });
                  }}
                />
                The policy must stop anyone who committed approving
              </label>
              <div className="max-w-xs">
                <SeveritySelect
                  label="Severity"
                  value={approval.severity}
                  disabled={false}
                  onChange={(severity) => {
                    onChange({ ...form, approval: { ...approval, severity } });
                  }}
                />
              </div>
            </div>
          </section>
        )}

        {(!scan.enabled || !approval.enabled) && (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-2">
              {!scan.enabled && (
                <button
                  type="button"
                  onClick={() => {
                    onChange({
                      ...form,
                      scan: { ...offScanRule(), enabled: true },
                    });
                  }}
                  className={secondaryClass}
                >
                  Add rule: required scans
                </button>
              )}
              {!approval.enabled && (
                <button
                  type="button"
                  onClick={() => {
                    onChange({
                      ...form,
                      approval: { ...offApprovalRule(), enabled: true },
                    });
                  }}
                  className={secondaryClass}
                >
                  Add rule: required approvals
                </button>
              )}
            </div>
            <p className="text-sm text-slate-600">
              These rules check GitLab security policies, which need GitLab Ultimate. Where Baluarte
              can&apos;t read them, results are unknown, never passing.
            </p>
          </div>
        )}
      </fieldset>

      <fieldset className="space-y-2">
        <legend className="text-base font-semibold">Groups it covers</legend>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name={`${id}-scope`}
            checked={form.scope === "all"}
            onChange={() => {
              onChange({ ...form, scope: "all" });
            }}
          />
          Every group and project it is applied to
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name={`${id}-scope`}
            checked={form.scope === "groups"}
            onChange={() => {
              onChange({ ...form, scope: "groups" });
            }}
          />
          Only some groups
        </label>
        {form.scope === "groups" && (
          <label className="block pl-6 text-sm font-medium" htmlFor={`${id}-groups`}>
            Group paths, one per line
            <textarea
              id={`${id}-groups`}
              value={form.groups.join("\n")}
              rows={3}
              aria-describedby={`${id}-groups-help`}
              onChange={(event) => {
                onChange({ ...form, groups: event.target.value.split("\n") });
              }}
              className={`${fieldClass} font-mono`}
            />
            <span id={`${id}-groups-help`} className="mt-1 block font-normal text-slate-600">
              A group also covers its subgroups. Elsewhere the baseline is not checked, even where
              it is applied.
            </span>
          </label>
        )}
      </fieldset>
    </div>
  );
}
