import { useId, useState, type ChangeEvent, type FormEvent } from "react";

import type { ApiError } from "../api/client";
import {
  saveGitLab,
  testGitLab,
  type CheckStatus,
  type ConfigBase,
  type GitLabConfig,
  type GitLabTestResult,
} from "../api/setup";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { formatTime } from "../time";
import {
  DEFAULT_SYNC_INTERVAL_MINUTES,
  DEFAULT_TIMEOUT_SECONDS,
  gitlabProblems,
  type GitLabFields,
} from "./format";
import { asApiError, errorClass, noteClass, Problems, SecretKeyNotice } from "./parts";

const checkLabels: Record<CheckStatus, string> = { pass: "Pass", warn: "Warning", fail: "Fail" };
const checkClasses: Record<CheckStatus, string> = {
  pass: "bg-emerald-100 text-emerald-900",
  warn: "bg-amber-100 text-amber-950",
  fail: "bg-red-100 text-red-900",
};

export function GitLabTestResultView({ result }: { result: GitLabTestResult }) {
  return (
    <div
      className={`space-y-2 rounded border p-3 text-sm ${
        result.ok
          ? "border-emerald-300 bg-emerald-50 text-emerald-950"
          : "border-red-300 bg-red-50 text-red-900"
      }`}
    >
      <p className="font-medium">{result.ok ? "Connection succeeded" : "Connection failed"}</p>
      {result.error && <p>{result.error}</p>}
      <ul className="list-disc pl-5">
        {result.version && <li>GitLab version: {result.version}</li>}
        {result.user && <li>Token user: {result.user.username}</li>}
        {result.token_scopes && (
          <li>
            Token scopes: {result.token_scopes.length > 0 ? result.token_scopes.join(", ") : "none"}
          </li>
        )}
        {result.token_scopes !== null && (
          <li>
            Token expires:{" "}
            {result.token_expires_at
              ? formatTime(result.token_expires_at)
              : "never (no expiry set)"}
          </li>
        )}
      </ul>
      {result.user?.is_admin && (
        <p className={noteClass}>
          This token belongs to a GitLab administrator. Baluarte needs only <code>read_api</code>;
          use a dedicated non-admin auditor account so the token cannot see or reach more than it
          should.
        </p>
      )}
      {result.checks.length > 0 && (
        <ul className="space-y-1">
          {result.checks.map((check) => (
            <li key={check.name} className="flex flex-wrap items-baseline gap-2">
              <span
                className={`rounded px-1.5 py-0.5 text-xs font-medium ${checkClasses[check.status]}`}
              >
                {checkLabels[check.status]}
              </span>
              <span className="font-medium">{check.name}:</span>
              <span>{check.message}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function initialFields(config: GitLabConfig | null): GitLabFields {
  return {
    url: config?.url ?? "",
    token: "",
    timeout_seconds: String(config?.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS),
    sync_interval_minutes: String(config?.sync_interval_minutes ?? DEFAULT_SYNC_INTERVAL_MINUTES),
  };
}

export function GitLabForm({
  base,
  csrf,
  config,
  secretKeyConfigured,
  onSaved,
  onError,
  submitLabel = "Save GitLab connection",
  onCancel,
}: {
  base: ConfigBase;
  csrf: string;
  config: GitLabConfig | null;
  secretKeyConfigured: boolean | null;
  onSaved: (config: GitLabConfig) => void;
  /** Called with every failed request, after its message is shown. */
  onError: (error: ApiError) => void;
  submitLabel?: string;
  onCancel?: () => void;
}) {
  const id = useId();
  const [fields, setFields] = useState<GitLabFields>(() => initialFields(config));
  const [problems, setProblems] = useState<string[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<GitLabTestResult | null>(null);
  const [testFailure, setTestFailure] = useState<string | null>(null);
  const tokenStored = config?.token === "configured";
  const canStoreSecret = secretKeyConfigured !== false;

  function change(event: ChangeEvent<HTMLInputElement>) {
    const { name, value } = event.currentTarget;
    setFields((current) => ({ ...current, [name]: value }));
  }

  async function runTest() {
    setTest(null);
    setTestFailure(null);
    if (!fields.url.trim()) {
      setTestFailure("Enter the GitLab address first.");
      return;
    }
    if (!fields.token.trim() && !tokenStored) {
      setTestFailure("Enter a token first.");
      return;
    }
    setTesting(true);
    try {
      setTest(
        await testGitLab(
          base,
          { url: fields.url.trim(), token: fields.token.trim() || null },
          csrf,
        ),
      );
    } catch (error) {
      const apiError = asApiError(error);
      setTestFailure(apiError.message);
      onError(apiError);
    } finally {
      setTesting(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found = gitlabProblems(fields, tokenStored);
    setProblems(found);
    setFailure(null);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const saved = await saveGitLab(
        base,
        {
          url: fields.url.trim().replace(/\/+$/, ""),
          token: fields.token.trim() || null,
          timeout_seconds: Number(fields.timeout_seconds),
          sync_interval_minutes: Number(fields.sync_interval_minutes),
        },
        csrf,
      );
      setFields((current) => ({ ...current, token: "" }));
      onSaved(saved);
    } catch (error) {
      const apiError = asApiError(error);
      setFailure(apiError.message);
      onError(apiError);
    } finally {
      setSaving(false);
    }
  }

  if (!canStoreSecret && !tokenStored) {
    return <SecretKeyNotice />;
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="GitLab connection"
      noValidate
      className="space-y-4"
    >
      <Problems messages={failure ? [failure] : problems} />
      <label className="block text-sm font-medium">
        GitLab address
        <input
          name="url"
          type="url"
          value={fields.url}
          onChange={change}
          placeholder="https://gitlab.example.com"
          aria-describedby={`${id}-url-help`}
          className={fieldClass}
        />
      </label>
      <p id={`${id}-url-help`} className="text-xs text-slate-600">
        The https address of your self-managed GitLab, without <code>/api/v4</code>. Relative-URL
        installs such as <code>https://example.com/gitlab</code> work.
      </p>

      <label className="block text-sm font-medium">
        Access token
        <input
          name="token"
          type="password"
          value={fields.token}
          onChange={change}
          autoComplete="new-password"
          disabled={!canStoreSecret}
          aria-describedby={`${id}-token-help`}
          className={fieldClass}
        />
      </label>
      <p id={`${id}-token-help`} className="text-xs text-slate-600">
        {tokenStored
          ? "A token is stored and is never shown. Leave this blank to keep it, or enter a new one to replace it. "
          : ""}
        Use a token with only the <code>read_api</code> scope and an expiry date, from a dedicated
        non-admin account. Baluarte only reads from GitLab.
      </p>
      {!canStoreSecret && <SecretKeyNotice />}

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block text-sm font-medium">
          Request timeout (seconds)
          <input
            name="timeout_seconds"
            type="number"
            min={1}
            max={120}
            value={fields.timeout_seconds}
            onChange={change}
            className={fieldClass}
          />
        </label>
        <label className="block text-sm font-medium">
          Collect every (minutes)
          <input
            name="sync_interval_minutes"
            type="number"
            min={0}
            max={10080}
            value={fields.sync_interval_minutes}
            onChange={change}
            aria-describedby={`${id}-interval-help`}
            className={fieldClass}
          />
        </label>
      </div>
      <p id={`${id}-interval-help`} className="text-xs text-slate-600">
        0 turns scheduled collection off; otherwise 15 to 10080 minutes (one week). The default is
        360 (six hours).
      </p>

      <div className="space-y-2">
        <button
          type="button"
          onClick={() => void runTest()}
          disabled={testing}
          className={secondaryClass}
        >
          {testing ? "Testing…" : "Test connection"}
        </button>
        <div aria-live="polite" className="space-y-2">
          {testFailure && <p className={errorClass}>{testFailure}</p>}
          {test && <GitLabTestResultView result={test} />}
        </div>
      </div>

      <div className="flex gap-2">
        <button type="submit" disabled={saving} className={buttonClass}>
          {saving ? "Saving…" : submitLabel}
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} className={secondaryClass}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
