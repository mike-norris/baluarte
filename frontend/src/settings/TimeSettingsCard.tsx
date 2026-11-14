import { useState, type ChangeEvent, type FormEvent } from "react";

import { errorReasons } from "../api/send";
import { getTimeSettings, saveTimeSettings, TIME_LIMITS, type TimeSettings } from "../api/time";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { asApiError, cardClass, Problems } from "./parts";

interface Fields {
  lookback_days: string;
  reread_days: string;
  retention_days: string;
}

function fieldsOf(settings: TimeSettings): Fields {
  return {
    lookback_days: String(settings.lookback_days),
    reread_days: String(settings.reread_days),
    retention_days: settings.retention_days === null ? "" : String(settings.retention_days),
  };
}

function whole(value: string): number | null {
  const text = value.trim();
  return /^\d+$/.test(text) ? Number(text) : null;
}

/** Problems with the time settings, in field order; empty when they may be sent. */
export function timeSettingsProblems(fields: Fields): string[] {
  const problems: string[] = [];
  const check = (value: string, limits: { min: number; max: number }, what: string) => {
    const number = whole(value);
    if (number === null || number < limits.min || number > limits.max) {
      problems.push(
        `${what} must be a whole number from ${String(limits.min)} to ${String(limits.max)}.`,
      );
    }
  };
  check(fields.lookback_days, TIME_LIMITS.lookback, "Lookback");
  check(fields.reread_days, TIME_LIMITS.reread, "Read again for");
  if (fields.retention_days.trim()) check(fields.retention_days, TIME_LIMITS.retention, "Keep for");
  return problems;
}

function TimeForm({
  csrf,
  settings,
  onSaved,
}: {
  csrf: string;
  settings: TimeSettings;
  onSaved: () => void;
}) {
  const { refresh } = useAuth();
  const [fields, setFields] = useState(() => fieldsOf(settings));
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  function change(event: ChangeEvent<HTMLInputElement>) {
    const { name, value } = event.currentTarget;
    setFields((current) => ({ ...current, [name]: value }));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found = timeSettingsProblems(fields);
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      await saveTimeSettings(
        {
          lookback_days: Number(fields.lookback_days.trim()),
          reread_days: Number(fields.reread_days.trim()),
          retention_days: fields.retention_days.trim()
            ? Number(fields.retention_days.trim())
            : null,
        },
        csrf,
      );
      onSaved();
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
      if (apiError.status === 401) void refresh();
    } finally {
      setSaving(false);
    }
  }

  const number = (
    name: keyof Fields,
    label: string,
    limits: { min: number; max: number },
    help: string,
  ) => (
    <div>
      <label className="d-block fw-medium">
        {label}
        <input
          name={name}
          type="number"
          min={limits.min}
          max={limits.max}
          step={1}
          value={fields[name]}
          onChange={change}
          aria-describedby={`${name}-help`}
          className="form-control mt-1"
        />
      </label>
      <p id={`${name}-help`} className="mt-1 small text-secondary">
        {help}
      </p>
    </div>
  );

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="Time tracking settings"
      noValidate

    >
      <Problems messages={problems} />
      <div className="grid gap-3 sm:grid-cols-3">
        {number(
          "lookback_days",
          "Lookback (days)",
          TIME_LIMITS.lookback,
          "How far back the first read of a project goes, 1 to 730 days.",
        )}
        {number(
          "reread_days",
          "Read again for (days)",
          TIME_LIMITS.reread,
          "Recent time logs are read again for this long, 1 to 90 days, so edits and removals in GitLab are noticed.",
        )}
        {number(
          "retention_days",
          "Keep for (days)",
          TIME_LIMITS.retention,
          "30 to 3650 days. Leave empty to keep time logs as long as other evidence.",
        )}
      </div>
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Saving…" : "Save time tracking settings"}
      </button>
    </form>
  );
}

/** How time logs are read and kept, and whether this deployment shows time per person. */
export function TimeSettingsCard({ csrf }: { csrf: string }) {
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const loaded = useLoad(`settings-time#${String(version)}`, (signal) => getTimeSettings(signal));

  return (
    <section aria-labelledby="time-settings-title" className={cardClass}>
      <h2 id="time-settings-title" className="card-title">
        Time tracking
      </h2>
      <p className="text-secondary">
        Time logs and estimates are read from GitLab with the read-only token. The text of a time
        log&apos;s summary is never stored. Every change to these settings is audited.
      </p>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : loaded.data ? (
        <>
          <p >
            {loaded.data.per_person_enabled
              ? "Time per person is shown to managers for their groups, and to administrators. Turn it off with BALUARTE_TIME_PER_PERSON=false in the deployment."
              : "Time per person is turned off for this deployment (BALUARTE_TIME_PER_PERSON=false). Only totals are shown."}
          </p>
          <TimeForm
            key={String(version)}
            csrf={csrf}
            settings={loaded.data}
            onSaved={() => {
              setMessage("Time tracking settings saved. They apply from the next collection.");
              setVersion((value) => value + 1);
            }}
          />
        </>
      ) : (
        <p role="status" className="text-secondary">
          Loading time tracking settings…
        </p>
      )}
      <p role="status" className="text-success-emphasis">
        {message ?? ""}
      </p>
    </section>
  );
}
