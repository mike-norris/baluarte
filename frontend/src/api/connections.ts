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

export function connectionName(connection: Connection): string {
  return connection.name ?? new URL(connection.url).host;
}

export function listConnections(signal?: AbortSignal): Promise<{ connections: Connection[] }> {
  return getJson("/api/v1/connections", undefined, signal);
}

export async function addConnection(body: NewConnection, csrf: string): Promise<Connection> {
  return (await sendJson<Connection>("POST", "/api/v1/connections", body, csrf)) as Connection;
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
