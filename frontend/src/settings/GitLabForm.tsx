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
  pass: "bg-success-subtle text-success-emphasis border border-success-subtle",
  warn: "bg-warning-subtle text-warning-emphasis border border-warning-subtle",
  fail: "bg-danger-subtle text-danger-emphasis border border-danger-subtle",
};

// Scopes that let a token change GitLab; admin_mode alone only unlocks admin reads with read_api.
const WRITE_SCOPES = new Set([
  "api",
  "write_repository",
  "write_registry",
  "sudo",
  "create_runner",
  "manage_runner",
  "k8s_proxy",
  "ai_features",
]);

function readOnly(scopes: string[] | null): boolean {
  return scopes !== null && scopes.includes("read_api") && !scopes.some((s) => WRITE_SCOPES.has(s));
}

export function GitLabTestResultView({ result }: { result: GitLabTestResult }) {
  return (
    <div
      className={`alert text-body ${result.ok ? "alert-success" : "alert-danger"}`}
    >
      <p className="fw-medium">{result.ok ? "Connection succeeded" : "Connection failed"}</p>
      {result.error && <p>{result.error}</p>}
      <ul className="ps-3">
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
      {result.user?.is_admin && !readOnly(result.token_scopes) && (
        <p className={noteClass}>
          This token belongs to a GitLab administrator and can change GitLab, or its scopes could
          not be read. Issue it with <code>read_api</code> only (add <code>admin_mode</code> to
          cover the whole instance).
        </p>
      )}
      {result.checks.length > 0 && (
        <ul >
          {result.checks.map((check) => (
            <li key={check.name} className="d-flex flex-wrap align-items-baseline gap-2">
              <span
                className={`rounded px-1.5 py-0.5 text-xs font-medium ${checkClasses[check.status]}`}
              >
                {checkLabels[check.status]}
              </span>
              <span className="fw-medium">{check.name}:</span>
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

    >
      <Problems messages={failure ? [failure] : problems} />
      <label className="d-block fw-medium">
        GitLab address
        <input
          name="url"
          type="url"
          value={fields.url}
          onChange={change}
          placeholder="https://gitlab.example.com"
          aria-describedby={`${id}-url-help`}
          className="form-control mt-1"
        />
      </label>
      <p id={`${id}-url-help`} className="small text-secondary">
        The https address of your self-managed GitLab, without <code>/api/v4</code>. Relative-URL
        installs such as <code>https://example.com/gitlab</code> work.
      </p>

      <label className="d-block fw-medium">
        Access token
        <input
          name="token"
          type="password"
          value={fields.token}
          onChange={change}
          autoComplete="new-password"
          disabled={!canStoreSecret}
          aria-describedby={`${id}-token-help`}
          className="form-control mt-1"
        />
      </label>
      <p id={`${id}-token-help`} className="small text-secondary">
        {tokenStored
          ? "A token is stored and is never shown. Leave this blank to keep it, or enter a new one to replace it. "
          : ""}
        Use a token with only the <code>read_api</code> scope and an expiry date, from a dedicated
        non-admin account. Baluarte only reads from GitLab.
      </p>
      {!canStoreSecret && <SecretKeyNotice />}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="d-block fw-medium">
          Request timeout (seconds)
          <input
            name="timeout_seconds"
            type="number"
            min={1}
            max={120}
            value={fields.timeout_seconds}
            onChange={change}
            className="form-control mt-1"
          />
        </label>
        <label className="d-block fw-medium">
          Collect every (minutes)
          <input
            name="sync_interval_minutes"
            type="number"
            min={0}
            max={10080}
            value={fields.sync_interval_minutes}
            onChange={change}
            aria-describedby={`${id}-interval-help`}
            className="form-control mt-1"
          />
        </label>
      </div>
      <p id={`${id}-interval-help`} className="small text-secondary">
        0 turns scheduled collection off; otherwise 15 to 10080 minutes (one week). The default is
        360 (six hours).
      </p>

      <div >
        <button
          type="button"
          onClick={() => void runTest()}
          disabled={testing}
          className="btn"
        >
          {testing ? "Testing…" : "Test connection"}
        </button>
        <div aria-live="polite" >
          {testFailure && <p className={errorClass}>{testFailure}</p>}
          {test && <GitLabTestResultView result={test} />}
        </div>
      </div>

      <div className="d-flex gap-2">
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
