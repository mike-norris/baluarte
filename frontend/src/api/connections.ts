/** Connections: the GitLab deployments Baluarte collects from (roadmap M11.3). */

import { getJson } from "./client";
import type { Freshness, KindFreshness } from "./gitlab";
import { sendJson } from "./send";

export type ConnectionSource = "deployment" | "settings_page" | "connections_page";

export interface Connection {
  id: number;
  provider: string;
  name: string | null;
  url: string;
  /** Set in the deployment or on the Settings page. */
  primary: boolean;
  source: ConnectionSource;
  paused: boolean;
  token: "configured" | "not_set";
  timeout_seconds: number;
  sync_interval_minutes: number;
  allow_private_network: boolean;
  /** ``token``, or for GitHub ``app`` (an App installation). */
  auth_kind?: "token" | "app";
  app_id?: number | null;
  installation_id?: number | null;
  version: string | null;
  edition: string | null;
  created_at: string;
  health: Freshness;
  collections: Record<string, KindFreshness>;
}

export interface NewConnection {
  name: string;
  url: string;
  token: string;
  timeout_seconds: number;
  sync_interval_minutes: number;
  allow_private_network: boolean;
}

/** A GitHub.com organization, read with a fine-grained token or a GitHub App installation. */
export interface NewGitHubConnection {
  name: string;
  organization: string;
  auth_kind: "token" | "app";
  /** The fine-grained token, or the App's private key (PEM). */
  secret: string;
  app_id?: number;
  installation_id?: number;
  timeout_seconds: number;
  sync_interval_minutes: number;
}

export type ConnectionChange = Partial<Omit<NewConnection, "url">> & { paused?: boolean };

export interface DiagnosticCheck {
  id: string;
  status: "ok" | "warning" | "failed" | "unknown";
  summary: string;
}

export interface Diagnostics {
  status: "ok" | "warning" | "failed";
  checked_at: string;
  gitlab_url: string | null;
  version: string | null;
  checks: DiagnosticCheck[];
}

export const sourceLabels: Record<ConnectionSource, string> = {
  deployment: "Set in the deployment",
  settings_page: "Set on the Settings page",
  connections_page: "Added here",
};

export const providerLabels: Record<string, string> = { gitlab: "GitLab", github: "GitHub" };

export function connectionName(connection: Connection): string {
  if (connection.name) return connection.name;
  const url = new URL(connection.url);
  // A GitHub connection is one organization on github.com.
  return connection.provider === "github" ? `${url.host}${url.pathname}` : url.host;
}

export function listConnections(signal?: AbortSignal): Promise<{ connections: Connection[] }> {
  return getJson("/api/v1/connections", undefined, signal);
}

export async function addConnection(body: NewConnection, csrf: string): Promise<Connection> {
  return (await sendJson<Connection>("POST", "/api/v1/connections", body, csrf)) as Connection;
}

export async function addGitHubConnection(
  body: NewGitHubConnection,
  csrf: string,
): Promise<Connection> {
  return (await sendJson<Connection>(
    "POST",
    "/api/v1/connections/github",
    body,
    csrf,
  )) as Connection;
}

export async function changeConnection(
  id: number,
  body: ConnectionChange,
  csrf: string,
): Promise<Connection> {
  return (await sendJson<Connection>(
    "PATCH",
    `/api/v1/connections/${String(id)}`,
    body,
    csrf,
  )) as Connection;
}

export async function removeConnection(id: number, csrf: string): Promise<void> {
  await sendJson("DELETE", `/api/v1/connections/${String(id)}`, undefined, csrf);
}

export async function testConnection(id: number, csrf: string): Promise<Diagnostics> {
  return (await sendJson<Diagnostics>(
    "POST",
    `/api/v1/connections/${String(id)}/diagnostics`,
    undefined,
    csrf,
  )) as Diagnostics;
}

export async function collectConnection(id: number, csrf: string): Promise<void> {
  await sendJson("POST", `/api/v1/connections/${String(id)}/collect`, undefined, csrf);
}

/** A delivery a GitHub connection's webhook accepted (roadmap M13.4b). */
export interface WebhookDelivery {
  delivery_id: string;
  event: string;
  action: string | null;
  repository_id: number | null;
  status: "queued" | "processed" | "ignored" | "failed";
  received_at: string;
  processed_at: string | null;
  error: string | null;
}

export interface Webhook {
  configured: boolean;
  /** The payload URL to give GitHub. */
  url: string;
  /** The events to subscribe the webhook to. */
  events: string[];
  deliveries: WebhookDelivery[];
}

/** Shown once, when the secret is set or rotated. */
export interface WebhookSecret {
  url: string;
  secret: string;
  events: string[];
}

export function readWebhook(id: number, signal?: AbortSignal): Promise<Webhook> {
  return getJson(`/api/v1/connections/${String(id)}/webhook`, undefined, signal);
}

export async function setWebhook(id: number, csrf: string): Promise<WebhookSecret> {
  return (await sendJson<WebhookSecret>(
    "POST",
    `/api/v1/connections/${String(id)}/webhook`,
    undefined,
    csrf,
  )) as WebhookSecret;
}

export async function removeWebhook(id: number, csrf: string): Promise<void> {
  await sendJson("DELETE", `/api/v1/connections/${String(id)}/webhook`, undefined, csrf);
}
