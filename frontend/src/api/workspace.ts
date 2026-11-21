/** Workspaces and the connections in them (roadmap M11.4). */

import { getJson } from "./client";
import { sendJson } from "./send";

export { choose, chosenConnection, chosenWorkspace, scopeHeaders } from "./workspaceChoice";

export interface ConnectionChoice {
  id: number;
  name: string | null;
  url: string;
  primary: boolean;
}

export type WorkspaceRole = "admin" | "analyst" | "auditor" | "manager";

export interface WorkspaceInfo {
  slug: string;
  name: string;
  default: boolean;
  roles: WorkspaceRole[];
  group_roles: Partial<Record<WorkspaceRole, string[]>> | null;
  connections: ConnectionChoice[];
}

export interface WorkspaceList {
  workspaces: WorkspaceInfo[];
  can_manage: boolean;
}

export function listWorkspaces(signal?: AbortSignal): Promise<WorkspaceList> {
  return getJson("/api/v1/workspaces", undefined, signal);
}

export interface NewWorkspace {
  slug: string;
  name: string;
  group_roles: Partial<Record<WorkspaceRole, string[]>>;
}

export async function createWorkspace(body: NewWorkspace, csrf: string): Promise<WorkspaceInfo> {
  return (await sendJson<WorkspaceInfo>("POST", "/api/v1/workspaces", body, csrf)) as WorkspaceInfo;
}

export async function changeWorkspace(
  slug: string,
  body: Partial<Omit<NewWorkspace, "slug">>,
  csrf: string,
): Promise<WorkspaceInfo> {
  return (await sendJson<WorkspaceInfo>(
    "PATCH",
    `/api/v1/workspaces/${encodeURIComponent(slug)}`,
    body,
    csrf,
  )) as WorkspaceInfo;
}

export const WORKSPACE_SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
