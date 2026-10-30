import { useId, useState, type ChangeEvent, type FormEvent } from "react";

import {
  saveOidc,
  testOidc,
  type ConfigBase,
  type OidcConfig,
  type OidcTestResult,
} from "../api/setup";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import {
  DEFAULT_ROLES_CLAIM,
  DEFAULT_SCOPES,
  oidcProblems,
  parseGroups,
  redirectUri,
  type OidcFields,
} from "./format";
import { asApiError, CopyValue, errorClass, noteClass, Problems, SecretKeyNotice } from "./parts";

function initialFields(config: OidcConfig | null): OidcFields {
  return {
    public_url: config?.public_url ?? window.location.origin,
    issuer: config?.issuer ?? "",
    client_id: config?.client_id ?? "",
    client_secret: "",
    clear_client_secret: false,
    scopes: config?.scopes ?? DEFAULT_SCOPES,
    roles_claim: config?.roles_claim ?? DEFAULT_ROLES_CLAIM,
    admin_groups: config?.admin_groups.join("\n") ?? "",
    analyst_groups: config?.analyst_groups.join("\n") ?? "",
    auditor_groups: config?.auditor_groups.join("\n") ?? "",
  };
}

export function OidcTestResultView({ result }: { result: OidcTestResult }) {
  if (!result.ok) {
    return (
      <div className={errorClass}>
        <p className="font-medium">Discovery failed</p>
        <p className="mt-1">
          {result.error ?? "The issuer's discovery document could not be read."}
        </p>
      </div>
    );
  }
  const openid = result.scopes_supported.includes("openid");
  return (
    <div className="rounded border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-950">
      <p className="font-medium">Discovery succeeded</p>
      <ul className="mt-1 list-disc pl-5">
        <li>Issuer: {result.issuer}</li>
        <li>Sign-in page: {result.authorization_endpoint}</li>
        {result.scopes_supported.length > 0 && (
          <li>Scopes offered: {result.scopes_supported.join(", ")}</li>
        )}
      </ul>
      {result.scopes_supported.length > 0 && !openid && (
        <p className="mt-2 font-medium">The provider does not list the openid scope.</p>
      )}
    </div>
  );
}

/** Hints for using GitLab itself as the identity provider, the common case for Baluarte. */
function ProviderHints() {
  return (
    <details className="rounded border border-slate-200 p-3 text-sm">
      <summary className="cursor-pointer font-medium">
        Using GitLab as the identity provider
      </summary>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-slate-700">
        <li>
          In GitLab, add an application under <em>User settings → Applications</em> or a
          group&apos;s <em>Settings → Applications</em>, with the redirect URI below and the scopes{" "}
          <code>openid profile email</code>. Keep it confidential and copy its secret.
        </li>
        <li>
          The issuer is GitLab&apos;s address, for example <code>https://gitlab.example.com</code>.
        </li>
        <li>
          Keep the roles claim <code>groups</code>: GitLab lists the full paths of the user&apos;s
          groups there, such as <code>platform/security</code>. Enter group paths exactly; matching
          is case-sensitive.
        </li>
      </ul>
    </details>
  );
}

export function OidcForm({
  base,
  csrf,
  config,
  secretKeyConfigured,
  onSaved,
  onUnauthorized,
  submitLabel = "Save identity provider",
  lockoutWarning = false,
  onCancel,
}: {
  base: ConfigBase;
  csrf: string;
  config: OidcConfig | null;
  /** Null when unknown (an older server); saving a secret is then attempted. */
  secretKeyConfigured: boolean | null;
  onSaved: (config: OidcConfig) => void;
  onUnauthorized: () => void;
  submitLabel?: string;
  lockoutWarning?: boolean;
  onCancel?: () => void;
}) {
  const id = useId();
  const [fields, setFields] = useState<OidcFields>(() => initialFields(config));
  const [problems, setProblems] = useState<string[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<OidcTestResult | null>(null);
  const [testFailure, setTestFailure] = useState<string | null>(null);
  const secretStored = config?.client_secret === "configured";
  const canStoreSecret = secretKeyConfigured !== false;
  const redirect = redirectUri(fields.public_url);

  function change(event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) {
    const { name, value, type } = event.currentTarget;
    const checked = type === "checkbox" && (event.currentTarget as HTMLInputElement).checked;
    setFields((current) => ({ ...current, [name]: type === "checkbox" ? checked : value }));
  }

  async function runTest() {
    setTest(null);
    setTestFailure(null);
    if (!fields.issuer.trim()) {
      setTestFailure("Enter the issuer first.");
      return;
    }
    setTesting(true);
    try {
      setTest(await testOidc(base, fields.issuer.trim(), csrf));
    } catch (error) {
      const apiError = asApiError(error);
      setTestFailure(apiError.message);
      if (apiError.status === 401) onUnauthorized();
    } finally {
      setTesting(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found = oidcProblems(fields);
    setProblems(found);
    setFailure(null);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const saved = await saveOidc(
        base,
        {
          public_url: fields.public_url.trim(),
          issuer: fields.issuer.trim(),
          client_id: fields.client_id.trim(),
          client_secret: fields.client_secret.trim() || null,
          clear_client_secret: fields.clear_client_secret,
          scopes: fields.scopes.trim(),
          roles_claim: fields.roles_claim.trim(),
          admin_groups: parseGroups(fields.admin_groups),
          analyst_groups: parseGroups(fields.analyst_groups),
          auditor_groups: parseGroups(fields.auditor_groups),
        },
        csrf,
      );
      setFields((current) => ({ ...current, client_secret: "", clear_client_secret: false }));
      onSaved(saved);
    } catch (error) {
      const apiError = asApiError(error);
      setFailure(apiError.message);
      if (apiError.status === 401) onUnauthorized();
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="Identity provider"
      noValidate
      className="space-y-4"
    >
      <Problems messages={failure ? [failure] : problems} />
      {lockoutWarning && (
        <p className={noteClass}>
          Changing the issuer, client or admin groups can lock every administrator out. Make sure
          you are in one of the admin groups, and keep this browser signed in until you have tested
          a new sign-in in another window.
        </p>
      )}
      <ProviderHints />

      <label className="block text-sm font-medium">
        Baluarte address
        <input
          name="public_url"
          type="url"
          value={fields.public_url}
          onChange={change}
          aria-describedby={`${id}-public-help`}
          className={fieldClass}
        />
      </label>
      <p id={`${id}-public-help`} className="text-xs text-slate-600">
        The address people use to open Baluarte, with no path. Plain http works only for localhost.
      </p>
      {redirect && <CopyValue label="Redirect URI to register" value={redirect} />}

      <div className="space-y-2">
        <label className="block text-sm font-medium">
          Issuer
          <input
            name="issuer"
            type="url"
            value={fields.issuer}
            onChange={change}
            placeholder="https://gitlab.example.com"
            aria-describedby={`${id}-issuer-help`}
            className={fieldClass}
          />
        </label>
        <p id={`${id}-issuer-help`} className="text-xs text-slate-600">
          Baluarte reads <code>/.well-known/openid-configuration</code> from this https URL.
        </p>
        <button
          type="button"
          onClick={() => void runTest()}
          disabled={testing}
          className={secondaryClass}
        >
          {testing ? "Testing…" : "Test discovery"}
        </button>
        <div aria-live="polite" className="space-y-2">
          {testFailure && <p className={errorClass}>{testFailure}</p>}
          {test && <OidcTestResultView result={test} />}
        </div>
      </div>

      <label className="block text-sm font-medium">
        Client ID
        <input
          name="client_id"
          value={fields.client_id}
          onChange={change}
          autoComplete="off"
          className={fieldClass}
        />
      </label>

      {canStoreSecret ? (
        <div className="space-y-2">
          <label className="block text-sm font-medium">
            Client secret
            <input
              name="client_secret"
              type="password"
              value={fields.client_secret}
              onChange={change}
              autoComplete="new-password"
              disabled={fields.clear_client_secret}
              aria-describedby={`${id}-secret-help`}
              className={fieldClass}
            />
          </label>
          <p id={`${id}-secret-help`} className="text-xs text-slate-600">
            {secretStored
              ? "A secret is stored and is never shown. Leave this blank to keep it, or enter a new one to replace it."
              : "Leave blank for a public client, which signs in with PKCE only."}
          </p>
          {secretStored && (
            <label className="flex items-center gap-2 text-sm">
              <input
                name="clear_client_secret"
                type="checkbox"
                checked={fields.clear_client_secret}
                onChange={change}
              />
              Remove the stored secret (public client)
            </label>
          )}
        </div>
      ) : (
        <SecretKeyNotice />
      )}

      <fieldset className="space-y-3 rounded border border-slate-200 p-3">
        <legend className="px-1 text-sm font-medium">Roles from identity provider groups</legend>
        <p className="text-xs text-slate-600">
          One group per line. A user gets every role whose groups they belong to; users in none of
          these groups cannot sign in.
        </p>
        <label className="block text-sm font-medium">
          Admin groups
          <textarea
            name="admin_groups"
            rows={2}
            value={fields.admin_groups}
            onChange={change}
            className={fieldClass}
          />
        </label>
        <label className="block text-sm font-medium">
          Analyst groups
          <textarea
            name="analyst_groups"
            rows={2}
            value={fields.analyst_groups}
            onChange={change}
            className={fieldClass}
          />
        </label>
        <label className="block text-sm font-medium">
          Auditor groups
          <textarea
            name="auditor_groups"
            rows={2}
            value={fields.auditor_groups}
            onChange={change}
            className={fieldClass}
          />
        </label>
      </fieldset>

      <details className="rounded border border-slate-200 p-3">
        <summary className="cursor-pointer text-sm font-medium">Advanced</summary>
        <div className="mt-3 space-y-3">
          <label className="block text-sm font-medium">
            Scopes
            <input name="scopes" value={fields.scopes} onChange={change} className={fieldClass} />
          </label>
          <label className="block text-sm font-medium">
            Roles claim
            <input
              name="roles_claim"
              value={fields.roles_claim}
              onChange={change}
              className={fieldClass}
            />
          </label>
          <p className="text-xs text-slate-600">
            The claim listing the user&apos;s groups, read from the ID token or userinfo. Add the
            scope your provider needs to release it.
          </p>
        </div>
      </details>

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
