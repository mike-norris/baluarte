import { useId, useState, type FormEvent, type ReactNode } from "react";

import { ApiError } from "../api/client";
import {
  BelowMinimum,
  saveProfile,
  type ComplianceSettings,
  type FrameworkId,
  type Profile,
  type ProfilePath,
  type SettingName,
  type Violation,
} from "../api/compliance";
import { fieldClass } from "../components/styles";
import { asApiError, noteClass, Problems } from "../settings/parts";
import {
  applyMinimums,
  defaultSettings,
  describeRequirement,
  frameworks,
  frameworkShort,
  meets,
  settingInfo,
  settingOrder,
  strictest,
} from "./format";

type Draft = Record<SettingName, string | boolean>;

function toDraft(settings: ComplianceSettings): Draft {
  const draft = {} as Draft;
  for (const name of settingOrder) {
    const value = settings[name];
    draft[name] = typeof value === "boolean" ? value : String(value);
  }
  return draft;
}

/** The draft as settings; NaN for a number field that is not a whole number. */
function fromDraft(draft: Draft): ComplianceSettings {
  const settings = {} as Record<SettingName, number | boolean>;
  for (const name of settingOrder) {
    const value = draft[name];
    settings[name] =
      typeof value === "boolean" ? value : /^\d+$/.test(value.trim()) ? Number(value) : NaN;
  }
  return settings as unknown as ComplianceSettings;
}

export function ViolationList({
  violations,
  action,
}: {
  violations: Violation[];
  action?: ((violation: Violation) => ReactNode) | undefined;
}) {
  return (
    <div role="alert" className="alert alert-danger text-body">
      <p className="fw-medium text-danger-emphasis">
        Not saved: these values do not meet the profile, and no exception is in force for them.
      </p>
      <ul >
        {violations.map((violation) => (
          <li key={violation.control_id} >
            <p className="text-danger-emphasis">
              {`${settingInfo[violation.setting].label}: ${String(violation.value)}. ${describeRequirement(violation.setting, violation.required)} (${violation.frameworks.map(frameworkShort).join(", ")}).`}
            </p>
            {action?.(violation)}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The operating profile: which frameworks Baluarte itself must meet, and the settings they
 * constrain. Shared by the setup wizard (minimums enforced here) and Settings, where a value
 * below a minimum is sent so the API can accept it under an exception or list what is missing.
 */
export function ComplianceForm({
  path,
  csrf,
  profile,
  enforceMinimums,
  submitLabel = "Save",
  onSaved,
  onError,
  onCancel,
  violationAction,
}: {
  path: ProfilePath;
  csrf: string;
  profile: Profile | null;
  enforceMinimums: boolean;
  submitLabel?: string;
  onSaved: (profile: Profile) => void;
  onError?: (error: ApiError) => void;
  onCancel?: () => void;
  violationAction?: (violation: Violation) => ReactNode;
}) {
  const configured = profile?.configured ?? false;
  const [selected, setSelected] = useState<FrameworkId[]>(
    configured ? (profile?.frameworks ?? []) : [],
  );
  const [none, setNone] = useState(configured && profile?.frameworks.length === 0);
  const [draft, setDraft] = useState<Draft>(() => toDraft(profile?.settings ?? defaultSettings));
  const [problems, setProblems] = useState<string[]>([]);
  const [violations, setViolations] = useState<Violation[]>([]);
  const [saving, setSaving] = useState(false);
  const formId = useId();

  const locked = (name: SettingName) => profile?.sources[name] === "deployment";

  function choose(next: FrameworkId[]) {
    setSelected(next);
    setNone(false);
    const current = fromDraft(draft);
    // Only raise valid numbers; an unfinished entry is left for the user to correct.
    const valid = settingOrder.every((name) => !Number.isNaN(current[name]));
    if (valid) setDraft(toDraft(applyMinimums(current, next, locked)));
  }

  function validate(settings: ComplianceSettings): string[] {
    const found: string[] = [];
    if (selected.length === 0 && !none) {
      found.push("Choose at least one framework, or None of these.");
    }
    for (const name of settingOrder) {
      const value = settings[name];
      const info = settingInfo[name];
      if (typeof value === "number") {
        if (Number.isNaN(value) || value < 1) {
          found.push(`${info.label}: enter a whole number of ${info.unit ?? ""} of 1 or more.`);
          continue;
        }
      }
      const requirement = strictest(selected, name);
      if (enforceMinimums && requirement && !locked(name) && !meets(value, requirement)) {
        found.push(`${info.label}: ${describeRequirement(name, requirement).toLowerCase()}.`);
      }
    }
    if (settings.session_idle_minutes > settings.session_lifetime_minutes) {
      found.push("The inactivity limit cannot be longer than the session limit.");
    }
    return found;
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const settings = fromDraft(draft);
    const found = validate(settings);
    setProblems(found);
    setViolations([]);
    if (found.length > 0) return;
    setSaving(true);
    try {
      onSaved(await saveProfile(path, { frameworks: none ? [] : selected, settings }, csrf));
    } catch (error) {
      if (error instanceof BelowMinimum) {
        setViolations(error.violations);
      } else {
        const apiError = asApiError(error);
        setProblems([apiError.message]);
        onError?.(apiError);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="space-y-5">
      <Problems messages={problems} />
      {violations.length > 0 && <ViolationList violations={violations} action={violationAction} />}
      <fieldset >
        <legend className="fw-semibold">Frameworks Baluarte must meet</legend>
        <p className="text-secondary">
          This governs how Baluarte itself operates: how long it keeps its audit log and evidence,
          how long sessions last, and whether baseline changes need a second person. It does not
          change the baselines Baluarte checks your GitLab projects against.
        </p>
        {frameworks.map((framework) => (
          <label key={framework.id} className="d-flex align-items-center gap-2">
            <input
              type="checkbox"
              checked={selected.includes(framework.id)}
              onChange={(event) => {
                choose(
                  event.target.checked
                    ? [...selected, framework.id]
                    : selected.filter((id) => id !== framework.id),
                );
              }}
              className="h-4 w-4"
            />
            {framework.name}
          </label>
        ))}
        <label className="d-flex align-items-center gap-2">
          <input
            type="checkbox"
            checked={none}
            onChange={(event) => {
              setNone(event.target.checked);
              if (event.target.checked) setSelected([]);
            }}
            className="h-4 w-4"
          />
          None of these (standard defaults; nothing is enforced)
        </label>
        {selected.length > 1 && (
          <p className="small text-secondary">
            Where frameworks differ, the strictest value applies.
          </p>
        )}
      </fieldset>

      <fieldset >
        <legend className="fw-semibold">Operating settings</legend>
        {settingOrder.map((name) => {
          const info = settingInfo[name];
          const value = draft[name];
          const requirement = strictest(selected, name);
          const fixed = locked(name);
          const parsed = fromDraft(draft)[name];
          const below =
            requirement !== null && !Number.isNaN(parsed) && !meets(parsed, requirement);
          const fieldId = `${formId}-${name}`;
          const described = `${fieldId}-hint${below ? ` ${fieldId}-warning` : ""}`;
          const hint = (
            <p id={`${fieldId}-hint`} className="small text-secondary">
              {info.help}
              {requirement &&
                ` ${describeRequirement(name, requirement)} for ${requirement.frameworks.map(frameworkShort).join(", ")}.`}
              {fixed && " Set by the deployment (environment variable); change it there."}
            </p>
          );
          const warning = below && (
            <p id={`${fieldId}-warning`} className="small fw-medium text-danger-emphasis">
              {fixed
                ? "The deployment's value does not meet the profile; the Compliance page shows this control as not met."
                : enforceMinimums
                  ? `Must be ${describeRequirement(name, requirement).toLowerCase()}.`
                  : "Below the profile's minimum: saving needs an exception in force for this control."}
            </p>
          );
          if (typeof value === "boolean") {
            return (
              <div key={name} >
                <label className="d-flex align-items-start gap-2 fw-medium">
                  <input
                    type="checkbox"
                    aria-describedby={described}
                    checked={value}
                    disabled={fixed}
                    onChange={(event) => {
                      setDraft({ ...draft, [name]: event.target.checked });
                    }}
                    className="mt-0.5 h-4 w-4"
                  />
                  {info.label}
                </label>
                <div className="pl-6">
                  {hint}
                  {warning}
                </div>
                {name === "legal_hold" && value && (
                  <p className={noteClass}>
                    While legal hold is on, the retention job deletes nothing, so the database keeps
                    growing. Turn it off when the hold ends; the next run then deletes what is past
                    its retention period.
                  </p>
                )}
              </div>
            );
          }
          return (
            <div key={name}>
              <label htmlFor={fieldId} className="d-block fw-medium">
                {`${info.label} (${info.unit ?? ""})`}
              </label>
              <input
                id={fieldId}
                aria-describedby={described}
                name={name}
                inputMode="numeric"
                value={value}
                disabled={fixed}
                onChange={(event) => {
                  setDraft({ ...draft, [name]: event.currentTarget.value });
                }}
                className={`${fieldClass} max-w-40`}
              />
              {hint}
              {warning}
            </div>
          );
        })}
      </fieldset>

      <div className="d-flex flex-wrap gap-2">
        <button type="submit" disabled={saving} className="btn btn-primary">
          {saving ? "Saving…" : submitLabel}
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} className="btn">
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
