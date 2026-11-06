import { useState, type ChangeEvent, type FormEvent } from "react";

import {
  getChangeEvidenceSettings,
  putChangeEvidenceSettings,
  type ChangeEvidenceSettings,
} from "../api/changes";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass } from "../components/styles";
import { changeEvidenceProblems, parseGroups, type ChangeEvidenceFields } from "./format";
import { asApiError, cardClass, Problems } from "./parts";

function fieldsOf(settings: ChangeEvidenceSettings): ChangeEvidenceFields {
  return {
    lookback_days: String(settings.lookback_days),
    redeploy_days: String(settings.redeploy_days),
    production_environments: settings.production_environments.join("\n"),
  };
}

function ChangeEvidenceForm({
  csrf,
  settings,
  onSaved,
}: {
  csrf: string;
  settings: ChangeEvidenceSettings;
  onSaved: (settings: ChangeEvidenceSettings) => void;
}) {
  const { refresh } = useAuth();
  const [fields, setFields] = useState(() => fieldsOf(settings));
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  function change(event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) {
    const { name, value } = event.currentTarget;
    setFields((current) => ({ ...current, [name]: value }));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found = changeEvidenceProblems(fields);
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      onSaved(
        await putChangeEvidenceSettings(
          {
            lookback_days: Number(fields.lookback_days.trim()),
            redeploy_days: Number(fields.redeploy_days.trim()),
            production_environments: parseGroups(fields.production_environments),
          },
          csrf,
        ),
      );
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
      if (apiError.status === 401) void refresh();
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="Change evidence settings"
      noValidate
      className="space-y-4"
    >
      <Problems messages={problems} />
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="block text-sm font-medium">
            Lookback (days)
            <input
              name="lookback_days"
              type="number"
              min={1}
              max={730}
              step={1}
              value={fields.lookback_days}
              onChange={change}
              aria-describedby="lookback-help"
              className={fieldClass}
            />
          </label>
          <p id="lookback-help" className="mt-1 text-xs text-slate-600">
            How far back the first read of a project goes, 1 to 730 days. Changes from before then
            are never recorded.
          </p>
        </div>
        <div>
          <label className="block text-sm font-medium">
            Read again for (days)
            <input
              name="redeploy_days"
              type="number"
              min={1}
              max={30}
              step={1}
              value={fields.redeploy_days}
              onChange={change}
              aria-describedby="redeploy-help"
              className={fieldClass}
            />
          </label>
          <p id="redeploy-help" className="mt-1 text-xs text-slate-600">
            Merged changes are read again for this long, 1 to 30 days, so a later deployment is
            recorded.
          </p>
        </div>
      </div>
      <div>
        <label className="block text-sm font-medium">
          Production environments
          <textarea
            name="production_environments"
            rows={3}
            value={fields.production_environments}
            onChange={change}
            aria-describedby="environments-help"
            className={fieldClass}
          />
        </label>
        <p id="environments-help" className="mt-1 text-xs text-slate-600">
          One per line or separated by commas, at most 20. Only successful deployments to these
          GitLab environments are recorded.
        </p>
      </div>
      <button type="submit" disabled={saving} className={buttonClass}>
        {saving ? "Saving…" : "Save change evidence settings"}
      </button>
    </form>
  );
}

/** How Baluarte reads changes: the lookback, the re-read period and production environments. */
export function ChangeEvidenceCard({ csrf }: { csrf: string }) {
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const loaded = useLoad(`settings-change-evidence#${String(version)}`, (signal) =>
    getChangeEvidenceSettings(signal),
  );

  return (
    <section aria-labelledby="change-evidence-title" className={cardClass}>
      <h2 id="change-evidence-title" className="text-lg font-semibold">
        Change evidence
      </h2>
      <p className="text-sm text-slate-600">
        Which merge requests, direct pushes and deployments are read for each monitored project.
        Every change to these settings is recorded in the audit log.
      </p>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : loaded.data ? (
        <ChangeEvidenceForm
          key={String(version)}
          csrf={csrf}
          settings={loaded.data}
          onSaved={() => {
            setMessage("Change evidence settings saved. They apply from the next collection.");
            setVersion((value) => value + 1);
          }}
        />
      ) : (
        <p role="status" className="text-sm text-slate-600">
          Loading change evidence settings…
        </p>
      )}
      <p role="status" className="text-sm text-emerald-800">
        {message ?? ""}
      </p>
    </section>
  );
}
