/** Configuration templates, their assignments and effective settings (docs/CONFIGURATION_TEMPLATES.md). */

import { ApiError, getJson } from "./client";
import type { PolicyError, Target, TargetType } from "./policies";
import { changeFailure, sendJson, sendJsonWithStatus } from "./send";
import { scopeHeaders } from "./workspaceChoice";

export type Support = "supported" | "unsupported" | "plan";

export interface TemplateVersionSummary {
  name: string;
  version: number;
  yaml_digest: string;
  /** ``name@version`` of the template it extends. */
  extends: string | null;
  created_by: string;
  created_at: string;
}

export interface TemplateSummary {
  name: string;
  description: string | null;
  /** Newest first. */
  versions: TemplateVersionSummary[];
}

export interface TemplateVersion extends TemplateVersionSummary {
  description: string | null;
  source_yaml: string;
  /** The template's own settings, one value per setting key. */
  settings: Record<string, unknown>;
  /** Its own settings and everything it inherits. */
  resolved_settings: Record<string, unknown>;
  /** The versions it extends, nearest first. */
  chain: string[];
}

export interface SettingSupport {
  key: string;
  gitlab: Support;
  gitlab_note: string | null;
  github: Support;
  github_note: string | null;
}

export interface TemplateAssignment {
  id: number;
  connection_id: number;
  name: string;
  version: number;
  target: Target;
  overrides: Record<string, unknown> | null;
  override_reason: string | null;
  assigned_by: string;
  created_at: string;
}

export interface TemplateAssignmentRequest {
  name: string;
  version: number;
  target_type: TargetType;
  target_id?: number;
  overrides?: Record<string, unknown>;
  override_reason?: string;
}

export interface Source {
  value: unknown;
  origin: "override" | "template" | "extends";
  template: string;
  version: number;
  assignment_id: number;
  assigned_to: Target;
}

export interface EffectiveSetting {
  key: string;
  value: unknown;
  rule_type: string;
  support: Support;
  support_note: string | null;
  decided_by: Source;
  replaced: Source[];
}

export interface EffectiveSettings {
  target: Target;
  provider: string;
  ancestry: "complete" | "incomplete";
  gaps: { gitlab_id: number; reason: string }[];
  path: Target[];
  assignments: {
    assignment_id: number;
    template: string;
    version: number;
    assigned_to: Target;
    override_reason: string | null;
    overrides: Record<string, unknown> | null;
  }[];
  settings: EffectiveSetting[];
}

export function listTemplates(signal?: AbortSignal): Promise<{ templates: TemplateSummary[] }> {
  return getJson("/api/v1/templates", undefined, signal);
}

export function getTemplateVersion(
  name: string,
  version: number,
  signal?: AbortSignal,
): Promise<TemplateVersion> {
  return getJson(
    `/api/v1/templates/${encodeURIComponent(name)}/versions/${String(version)}`,
    undefined,
    signal,
  );
}

export function listSettingSupport(signal?: AbortSignal): Promise<SettingSupport[]> {
  return getJson("/api/v1/templates/settings", undefined, signal);
}

export function listTemplateAssignments(
  signal?: AbortSignal,
): Promise<{ assignments: TemplateAssignment[] }> {
  return getJson("/api/v1/template-assignments", undefined, signal);
}

export function getEffectiveSettings(
  projectId: number,
  signal?: AbortSignal,
): Promise<EffectiveSettings> {
  return getJson(`/api/v1/templates/effective/${String(projectId)}`, undefined, signal);
}

export interface SavedTemplate {
  version: TemplateVersion;
  /** False when the source was identical to the latest version, which is returned instead. */
  created: boolean;
}

/** Save a template; a 422 with ``errors`` comes back as the problems found, not an exception. */
export async function saveTemplate(
  sourceYaml: string,
  csrfToken: string,
): Promise<SavedTemplate | { errors: PolicyError[] }> {
  let response: Response;
  try {
    response = await fetch("/api/v1/templates", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        ...scopeHeaders(),
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ source_yaml: sourceYaml }),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  const body = (await response.json().catch(() => null)) as {
    detail?: { errors?: PolicyError[] } | string;
  } | null;
  if (response.status === 422) {
    const detail = body?.detail;
    if (detail && typeof detail === "object" && Array.isArray(detail.errors)) {
      return { errors: detail.errors };
    }
    throw new ApiError(422, changeFailure(422, typeof detail === "string" ? detail : null));
  }
  if (!response.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : null;
    throw new ApiError(response.status, changeFailure(response.status, detail));
  }
  return { version: body as unknown as TemplateVersion, created: response.status === 201 };
}

export async function assignTemplate(
  request: TemplateAssignmentRequest,
  csrfToken: string,
): Promise<TemplateAssignment> {
  const { data } = await sendJsonWithStatus<TemplateAssignment>(
    "POST",
    "/api/v1/template-assignments",
    request,
    csrfToken,
  );
  if (data === null) throw new ApiError(204, "The API returned no content.");
  return data;
}

export async function unassignTemplate(id: number, csrfToken: string): Promise<void> {
  await sendJson("DELETE", `/api/v1/template-assignments/${String(id)}`, undefined, csrfToken);
}
