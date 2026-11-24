import { useId, useState, type ChangeEvent, type FormEvent } from "react";

import type { ApiError } from "../api/client";
import {
  saveOidc,
  testOidc,
  type ConfigBase,
  type OidcConfig,
  type OidcTestResult,
} from "../api/setup";
import {
  DEFAULT_ROLES_CLAIM,
  DEFAULT_SCOPES,
  oidcProblems,
  parseAmr,
  parseGroups,
  PRESETS,
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
    manager_groups: config?.manager_groups?.join("\n") ?? "",
    ...mfaFields(config),
  };
}

/** The MFA fields, leaving the overrides empty where they match the preset's own values. */
function mfaFields(
  config: OidcConfig | null,
): Pick<OidcFields, "preset" | "require_mfa" | "mfa_amr" | "acr_values"> {
  const preset = config?.preset ?? "generic";
  const defaults = PRESETS[preset];
  const amr = [...(config?.mfa_amr ?? defaults.amr)].sort();
  const sameAmr = amr.join(",") === [...defaults.amr].sort().join(",");
  const acr = config?.acr_values ?? null;
  return {
    preset,
    require_mfa: config?.require_mfa ?? false,
    mfa_amr: sameAmr ? "" : amr.join("\n"),
    acr_values: acr === defaults.acr ? "" : (acr ?? ""),
  };
}

export function OidcTestResultView({ result }: { result: OidcTestResult }) {
  if (!result.ok) {
    return (
      <div className={errorClass}>
        <p className="fw-medium">Discovery failed</p>
        <p className="mt-1">
          {result.error ?? "The issuer's discovery document could not be read."}
        </p>
      </div>
    );
  }
  const openid = result.scopes_supported.includes("openid");
  return (
    <div className="alert alert-success text-body">
      <p className="fw-medium">Discovery succeeded</p>
      <ul className="mt-1 ps-3">
        <li>Issuer: {result.issuer}</li>
        <li>Sign-in page: {result.authorization_endpoint}</li>
        {result.scopes_supported.length > 0 && (
          <li>Scopes offered: {result.scopes_supported.join(", ")}</li>
        )}
      </ul>
      {result.scopes_supported.length > 0 && !openid && (
        <p className="mt-2 fw-medium">The provider does not list the openid scope.</p>
      )}
    </div>
  );
}

/** Hints for using GitLab itself as the identity provider, the common case for Baluarte. */
function ProviderHints() {
  return (
    <details className="rounded border p-3">
      <summary className="fw-medium">Using GitLab as the identity provider</summary>
      <ul className="mt-2 ps-3 text-secondary stack-1">
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
  onError,
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
  /** Called with every failed request, after its message is shown. */
  onError: (error: ApiError) => void;
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

  function change(event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) {
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
      onError(apiError);
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
          manager_groups: parseGroups(fields.manager_groups),
          preset: fields.preset,
          require_mfa: fields.require_mfa,
          mfa_amr: parseAmr(fields.mfa_amr),
          acr_values: fields.acr_values.trim(),
        },
        csrf,
      );
      setFields((current) => ({ ...current, client_secret: "", clear_client_secret: false }));
      onSaved(saved);
    } catch (error) {
      const apiError = asApiError(error);
      setFailure(apiError.message);
      onError(apiError);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} aria-label="Identity provider" noValidate>
      <Problems messages={failure ? [failure] : problems} />
      {lockoutWarning && (
        <p className={noteClass}>
          Changing the issuer, client or admin groups can lock every administrator out. Make sure
          you are in one of the admin groups, and keep this browser signed in until you have tested
          a new sign-in in another window.
        </p>
      )}
      <ProviderHints />

      <label className="d-block fw-medium">
        Baluarte address
        <input
          name="public_url"
          type="url"
          value={fields.public_url}
          onChange={change}
          aria-describedby={`${id}-public-help`}
          className="form-control mt-1"
        />
      </label>
      <p id={`${id}-public-help`} className="small text-secondary">
        The address people use to open Baluarte, with no path. Plain http works only for localhost.
      </p>
      {redirect && <CopyValue label="Redirect URI to register" value={redirect} />}

      <div className="stack-2">
        <label className="d-block fw-medium">
          Issuer
          <input
            name="issuer"
            type="url"
            value={fields.issuer}
            onChange={change}
            placeholder="https://gitlab.example.com"
            aria-describedby={`${id}-issuer-help`}
            className="form-control mt-1"
          />
        </label>
        <p id={`${id}-issuer-help`} className="small text-secondary">
          Baluarte reads <code>/.well-known/openid-configuration</code> from this https URL.
        </p>
        <button type="button" onClick={() => void runTest()} disabled={testing} className="btn">
          {testing ? "Testing…" : "Test discovery"}
        </button>
        <div className="stack-2" aria-live="polite">
          {testFailure && <p className={errorClass}>{testFailure}</p>}
          {test && <OidcTestResultView result={test} />}
        </div>
      </div>

      <label className="d-block fw-medium">
        Client ID
        <input
          name="client_id"
          value={fields.client_id}
          onChange={change}
          autoComplete="off"
          className="form-control mt-1"
        />
      </label>

      {canStoreSecret ? (
        <div className="stack-2">
          <label className="d-block fw-medium">
            Client secret
            <input
              name="client_secret"
              type="password"
              value={fields.client_secret}
              onChange={change}
              autoComplete="new-password"
              disabled={fields.clear_client_secret}
              aria-describedby={`${id}-secret-help`}
              className="form-control mt-1"
            />
          </label>
          <p id={`${id}-secret-help`} className="small text-secondary">
            {secretStored
              ? "A secret is stored and is never shown. Leave this blank to keep it, or enter a new one to replace it."
              : "Leave blank for a public client, which signs in with PKCE only."}
          </p>
          {secretStored && (
            <label className="d-flex align-items-center gap-2">
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

      <fieldset className="rounded border p-3 stack-3">
        <legend className="px-1 fw-medium">Roles from identity provider groups</legend>
        <p className="small text-secondary">
          One group per line. A user gets every role whose groups they belong to; users in none of
          these groups cannot sign in.
        </p>
        <label className="d-block fw-medium">
          Admin groups
          <textarea
            name="admin_groups"
            rows={2}
            value={fields.admin_groups}
            onChange={change}
            className="form-control mt-1"
          />
        </label>
        <label className="d-block fw-medium">
          Analyst groups
          <textarea
            name="analyst_groups"
            rows={2}
            value={fields.analyst_groups}
            onChange={change}
            className="form-control mt-1"
          />
        </label>
        <label className="d-block fw-medium">
          Auditor groups
          <textarea
            name="auditor_groups"
            rows={2}
            value={fields.auditor_groups}
            onChange={change}
            className="form-control mt-1"
          />
        </label>
        <label className="d-block fw-medium">
          Manager groups
          <textarea
            name="manager_groups"
            rows={2}
            value={fields.manager_groups}
            onChange={change}
            aria-describedby="manager-groups-help"
            className="form-control mt-1"
          />
        </label>
        <p id="manager-groups-help" className="small text-secondary">
          Managers see the manager space and time tracking, and tag projects. They cannot change
          baselines or settings.
        </p>
      </fieldset>

      <fieldset className="rounded border p-3 stack-3">
        <legend className="px-1 fw-medium">Multi-factor authentication</legend>
        <label className="d-block fw-medium">
          Provider preset
          <select
            name="preset"
            value={fields.preset}
            onChange={change}
            aria-describedby={`${id}-preset-help`}
            className="form-select mt-1"
          >
            {(Object.keys(PRESETS) as (keyof typeof PRESETS)[]).map((key) => (
              <option key={key} value={key}>
                {PRESETS[key].label}
              </option>
            ))}
          </select>
        </label>
        <p id={`${id}-preset-help`} className="small text-secondary">
          How Baluarte asks the provider for MFA and reads it back from the ID token&apos;s{" "}
          <code>amr</code> claim. Okta and Auth0 report MFA as <code>mfa</code>.
        </p>
        <label className="d-flex align-items-center gap-2">
          <input
            name="require_mfa"
            type="checkbox"
            checked={fields.require_mfa}
            onChange={change}
            aria-describedby={`${id}-require-help`}
          />
          Require MFA
        </label>
        <p id={`${id}-require-help`} className="small text-secondary">
          Refuse a sign-in whose ID token does not prove a second factor. Require MFA in the
          provider first, and test a sign-in in another window before you sign out here.
        </p>
      </fieldset>

      <details className="rounded border p-3">
        <summary className="fw-medium">Advanced</summary>
        <div className="mt-3 stack-3">
          <label className="d-block fw-medium">
            Scopes
            <input
              name="scopes"
              value={fields.scopes}
              onChange={change}
              className="form-control mt-1"
            />
          </label>
          <label className="d-block fw-medium">
            Roles claim
            <input
              name="roles_claim"
              value={fields.roles_claim}
              onChange={change}
              className="form-control mt-1"
            />
          </label>
          <p className="small text-secondary">
            The claim listing the user&apos;s groups, read from the ID token or userinfo. Add the
            scope your provider needs to release it.
          </p>
          <label className="d-block fw-medium">
            amr values that prove MFA
            <input
              name="mfa_amr"
              value={fields.mfa_amr}
              onChange={change}
              placeholder={PRESETS[fields.preset].amr.join(", ")}
              className="form-control mt-1"
            />
          </label>
          <label className="d-block fw-medium">
            acr_values to ask for MFA
            <input
              name="acr_values"
              value={fields.acr_values}
              onChange={change}
              placeholder={PRESETS[fields.preset].acr ?? "none"}
              className="form-control mt-1"
            />
          </label>
          <p className="small text-secondary">Leave these empty to use the preset&apos;s.</p>
        </div>
      </details>

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
