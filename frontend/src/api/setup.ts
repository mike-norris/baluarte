/**
 * First-run setup and administrator settings (BAL-034). Secrets are write-only: the API reports
 * them as "configured" or "not_set" and never returns their values.
 */

import { ApiError, getJson } from "./client";
import { sendJson } from "./send";

export type SetupState = "needs_setup" | "awaiting_admin_sign_in" | "complete";
/** Where a section's configuration comes from; deployment configuration is read-only here. */
export type ConfigSource = "deployment" | "database";
export type SecretState = "configured" | "not_set";

export interface SectionStatus {
  source: ConfigSource | null;
  configured: boolean;
}

export interface SetupStatus {
  state: SetupState;
  setup_session: boolean;
  /** Null once setup is complete, so nothing more is disclosed. */
  secret_key_configured: boolean | null;
  oidc: SectionStatus | null;
  gitlab: SectionStatus | null;
}

export interface OidcConfig {
  source: ConfigSource;
  public_url: string;
  issuer: string;
  client_id: string;
  client_secret: SecretState;
  scopes: string;
  roles_claim: string;
  admin_groups: string[];
  analyst_groups: string[];
  auditor_groups: string[];
  redirect_uri: string;
}

export interface OidcUpdate {
  public_url: string;
  issuer: string;
  client_id: string;
  /** Null keeps the stored secret. */
  client_secret: string | null;
  clear_client_secret: boolean;
  scopes: string;
  roles_claim: string;
  admin_groups: string[];
  analyst_groups: string[];
  auditor_groups: string[];
}

export interface OidcTestResult {
  ok: boolean;
  issuer: string | null;
  authorization_endpoint: string | null;
  scopes_supported: string[];
  error: string | null;
}

export interface GitLabConfig {
  source: ConfigSource;
  url: string;
  token: SecretState;
  timeout_seconds: number;
  sync_interval_minutes: number;
}

export interface GitLabUpdate {
  url: string;
  /** Null keeps the stored token. */
  token: string | null;
  timeout_seconds: number;
  sync_interval_minutes: number;
}

export type CheckStatus = "pass" | "warn" | "fail";

export interface GitLabTestResult {
  ok: boolean;
  version: string | null;
  user: { username: string; is_admin: boolean } | null;
  token_scopes: string[] | null;
  token_expires_at: string | null;
  checks: { name: string; status: CheckStatus; message: string }[];
  error: string | null;
}

/** ``GET /api/v1/settings``: effective configuration with secrets reported only as set or not. */
export interface SettingsInfo {
  version: string;
  logging: { level: string; format: string } | null;
  database: {
    host: string | null;
    port: number | null;
    name: string | null;
    user: string | null;
    ssl_mode: string | null;
    password: SecretState;
  } | null;
  gitlab: {
    source?: ConfigSource;
    url: string;
    token: SecretState;
    timeout_seconds: number;
    sync_interval_minutes?: number;
  } | null;
  authentication: {
    source?: ConfigSource;
    public_url: string;
    issuer: string;
    client_id: string;
    client_secret: SecretState;
    scopes: string;
    roles_claim: string;
    group_roles: Partial<Record<string, string[]>>;
    session_idle_minutes: number;
    session_lifetime_minutes: number;
  };
  /** Absent on servers older than BAL-034. */
  secret_key?: SecretState;
}

/** The setup wizard and the Settings page share endpoints that differ only in their prefix. */
export type ConfigBase = "/api/v1/setup" | "/api/v1/settings";

export const SETUP: ConfigBase = "/api/v1/setup";
export const SETTINGS: ConfigBase = "/api/v1/settings";
export const CALLBACK_PATH = "/api/v1/auth/callback";

/** The setup status; servers older than BAL-034 have no setup, so a 404 means complete. */
export async function getSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  try {
    return await getJson<SetupStatus>("/api/v1/setup/status", undefined, signal);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return {
        state: "complete",
        setup_session: false,
        secret_key_configured: null,
        oidc: null,
        gitlab: null,
      };
    }
    throw error;
  }
}

/** Exchange the one-time setup code for a setup session; resolves to its CSRF token. */
export async function startSetupSession(code: string): Promise<string> {
  let response: Response;
  try {
    response = await fetch("/api/v1/setup/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (response.status === 401) throw new ApiError(401, "That setup code is not valid.");
  if (response.status === 429) {
    throw new ApiError(429, "Too many wrong codes. Wait 15 minutes and try again.");
  }
  if (response.status === 409) throw new ApiError(409, "Setup is already complete.");
  if (!response.ok) {
    throw new ApiError(response.status, `The API returned an error (${String(response.status)}).`);
  }
  const body = (await response.json()) as { csrf_token: string };
  return body.csrf_token;
}

export async function endSetupSession(csrfToken: string): Promise<void> {
  await sendJson("DELETE", "/api/v1/setup/session", undefined, csrfToken);
}

/** Null when nothing is stored yet (404). */
async function getOrNull<T>(path: string, signal?: AbortSignal): Promise<T | null> {
  try {
    return await getJson<T>(path, undefined, signal);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

async function required<T>(pending: Promise<T | null>): Promise<T> {
  const result = await pending;
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export function getOidcConfig(base: ConfigBase, signal?: AbortSignal): Promise<OidcConfig | null> {
  return getOrNull<OidcConfig>(`${base}/oidc`, signal);
}

export function testOidc(base: ConfigBase, issuer: string, csrf: string): Promise<OidcTestResult> {
  return required(sendJson<OidcTestResult>("POST", `${base}/oidc/test`, { issuer }, csrf));
}

export function saveOidc(base: ConfigBase, update: OidcUpdate, csrf: string): Promise<OidcConfig> {
  return required(sendJson<OidcConfig>("PUT", `${base}/oidc`, update, csrf));
}

export function getGitLabConfig(
  base: ConfigBase,
  signal?: AbortSignal,
): Promise<GitLabConfig | null> {
  return getOrNull<GitLabConfig>(`${base}/gitlab`, signal);
}

export function testGitLab(
  base: ConfigBase,
  request: { url: string; token: string | null },
  csrf: string,
): Promise<GitLabTestResult> {
  return required(sendJson<GitLabTestResult>("POST", `${base}/gitlab/test`, request, csrf));
}

export function saveGitLab(
  base: ConfigBase,
  update: GitLabUpdate,
  csrf: string,
): Promise<GitLabConfig> {
  return required(sendJson<GitLabConfig>("PUT", `${base}/gitlab`, update, csrf));
}

export async function disconnectGitLab(csrf: string): Promise<void> {
  await sendJson("DELETE", `${SETTINGS}/gitlab`, undefined, csrf);
}

export function getSettings(signal?: AbortSignal): Promise<SettingsInfo> {
  return getJson<SettingsInfo>(SETTINGS, undefined, signal);
}
