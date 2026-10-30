/** Checks for the identity provider and GitLab forms. The API repeats every check (BAL-034). */

import { CALLBACK_PATH } from "../api/setup";

export const DEFAULT_SCOPES = "openid profile email";
export const DEFAULT_ROLES_CLAIM = "groups";
export const DEFAULT_TIMEOUT_SECONDS = 15;
export const DEFAULT_SYNC_INTERVAL_MINUTES = 360;
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** One group per line or separated by commas; blanks and repeats dropped, order kept. */
export function parseGroups(text: string): string[] {
  const groups = text
    .split(/[\n,]/)
    .map((group) => group.trim())
    .filter(Boolean);
  return [...new Set(groups)];
}

function parseUrl(value: string): URL | null {
  try {
    return new URL(value.trim());
  } catch {
    return null;
  }
}

/** The origin Baluarte is served from, or null when it is not a valid public URL. */
export function publicOrigin(value: string): string | null {
  const url = parseUrl(value);
  if (!url || (url.protocol !== "https:" && url.protocol !== "http:")) return null;
  if (url.protocol === "http:" && !LOOPBACK.has(url.hostname)) return null;
  if ((url.pathname !== "/" && url.pathname !== "") || url.search || url.hash || url.username) {
    return null;
  }
  return url.origin;
}

export function redirectUri(publicUrl: string): string | null {
  const origin = publicOrigin(publicUrl);
  return origin ? origin + CALLBACK_PATH : null;
}

export function isHttpsUrl(value: string): boolean {
  const url = parseUrl(value);
  return url !== null && url.protocol === "https:" && url.hostname !== "" && !url.username;
}

export interface OidcFields {
  public_url: string;
  issuer: string;
  client_id: string;
  client_secret: string;
  clear_client_secret: boolean;
  scopes: string;
  roles_claim: string;
  admin_groups: string;
  analyst_groups: string;
  auditor_groups: string;
}

/** Problems with the identity provider form, in field order; empty when it may be sent. */
export function oidcProblems(fields: OidcFields): string[] {
  const problems: string[] = [];
  if (!publicOrigin(fields.public_url)) {
    problems.push(
      "Enter Baluarte's address as an origin with no path, such as https://baluarte.example.com. Plain http is allowed only for localhost.",
    );
  }
  if (!isHttpsUrl(fields.issuer)) problems.push("Enter the issuer as an https URL.");
  if (!fields.client_id.trim()) problems.push("Enter the client ID from your identity provider.");
  if (!fields.scopes.split(/\s+/).includes("openid")) problems.push("Scopes must include openid.");
  if (!fields.roles_claim.trim()) problems.push("Enter the claim that lists the user's groups.");
  if (parseGroups(fields.admin_groups).length === 0) {
    problems.push("Map at least one group to the admin role, or no one can manage Baluarte.");
  }
  return problems;
}

export interface GitLabFields {
  url: string;
  token: string;
  timeout_seconds: string;
  sync_interval_minutes: string;
}

/** Problems with the GitLab form; ``tokenStored`` allows leaving the token blank to keep it. */
export function gitlabProblems(fields: GitLabFields, tokenStored: boolean): string[] {
  const problems: string[] = [];
  const url = parseUrl(fields.url);
  if (!isHttpsUrl(fields.url) || url?.search || url?.hash) {
    problems.push("Enter the GitLab address as an https URL, without /api/v4.");
  }
  if (!tokenStored && !fields.token.trim()) problems.push("Enter a token with the read_api scope.");
  const timeout = Number(fields.timeout_seconds);
  if (!Number.isFinite(timeout) || timeout < 1 || timeout > 120) {
    problems.push("Choose a request timeout between 1 and 120 seconds.");
  }
  const interval = Number(fields.sync_interval_minutes);
  if (!Number.isInteger(interval) || (interval !== 0 && (interval < 15 || interval > 10080))) {
    problems.push("Choose a collection interval of 0 (off) or 15 to 10080 minutes.");
  }
  return problems;
}

export function describeInterval(minutes: number): string {
  if (minutes === 0) return "Off (collect only on request)";
  if (minutes % 1440 === 0) return `Every ${String(minutes / 1440)} day(s)`;
  if (minutes % 60 === 0) return `Every ${String(minutes / 60)} hour(s)`;
  return `Every ${String(minutes)} minutes`;
}
