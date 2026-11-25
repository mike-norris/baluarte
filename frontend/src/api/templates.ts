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
  /** Baluarte proposes change sets fixing drift of what this assignment decides. */
  auto_remediate: boolean;
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
  auto_remediate?: boolean;
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

// --- drift (M14.2) ---

export type DriftStatus = "aligned" | "drift" | "excepted" | "unknown" | "unsupported";

export interface ExceptionRef {
  id: number;
  state: string;
  expires_at: string;
  /** Approved and ending within 14 days. */
  expiring: boolean;
}

export interface SettingDrift {
  key: string;
  expected: unknown;
  status: DriftStatus;
  reason: string;
  rule_type: string;
  observed_at: string | null;
  decided_by: Source;
  exception: ExceptionRef | null;
}

export interface ProjectRef {
  id: number;
  gitlab_id: number;
  path: string;
}

export interface ProjectDrift {
  project: ProjectRef;
  connection_id: number;
  provider: string;
  ancestry: "complete" | "incomplete";
  evaluated_at: string;
  settings: SettingDrift[];
  counts: Partial<Record<DriftStatus, number>>;
  /** Null when no template applies. */
  status: "aligned" | "drift" | "unknown" | null;
}

export type StatusTally = Record<DriftStatus, number>;

export interface GroupTally {
  name: string;
  projects: number;
  projects_drifting: number;
  settings: StatusTally;
}

export interface Alignment {
  connection_id: number;
  provider: string;
  evaluated_at: string;
  projects: number;
  projects_with_templates: number;
  projects_aligned: number;
  projects_drifting: number;
  projects_unknown: number;
  settings: StatusTally;
  by_template: GroupTally[];
  by_group: GroupTally[];
  attention: {
    project: ProjectRef;
    status: "drift" | "unknown";
    counts: Partial<Record<DriftStatus, number>>;
  }[];
  attention_truncated: boolean;
  expiring_exceptions: number;
}

export type ExceptionState = "requested" | "approved" | "rejected" | "revoked" | "expired";

export interface UserRef {
  user_id: number;
  display_name: string | null;
}

export interface TemplateException {
  id: number;
  connection_id: number;
  project_id: number;
  project_path: string;
  setting: string;
  justification: string;
  state: ExceptionState;
  expiring: boolean;
  requested_by: UserRef;
  requested_at: string;
  expires_at: string;
  decided_by: UserRef | null;
  decided_at: string | null;
  decision_note: string | null;
  development_decision: boolean;
  ended_by: UserRef | null;
  ended_at: string | null;
  end_note: string | null;
}

export function getProjectDrift(projectId: number, signal?: AbortSignal): Promise<ProjectDrift> {
  return getJson(`/api/v1/templates/drift/${String(projectId)}`, undefined, signal);
}

export function getAlignment(signal?: AbortSignal): Promise<Alignment> {
  return getJson("/api/v1/templates/alignment", undefined, signal);
}

export function listTemplateExceptions(
  signal?: AbortSignal,
): Promise<{ exceptions: TemplateException[] }> {
  return getJson("/api/v1/template-exceptions", undefined, signal);
}

export interface TemplateExceptionRequest {
  project_id: number;
  setting: string;
  justification: string;
  expires_at: string;
}

export async function requestTemplateException(
  request: TemplateExceptionRequest,
  csrfToken: string,
): Promise<TemplateException> {
  const result = await sendJson<TemplateException>(
    "POST",
    "/api/v1/template-exceptions",
    request,
    csrfToken,
  );
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export async function decideTemplateException(
  id: number,
  action: "approve" | "reject" | "revoke",
  note: string | null,
  csrfToken: string,
): Promise<TemplateException> {
  const result = await sendJson<TemplateException>(
    "POST",
    `/api/v1/template-exceptions/${String(id)}/${action}`,
    note ? { note } : {},
    csrfToken,
  );
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}
