/**
 * Baluarte's own operating profile: the frameworks Baluarte itself must satisfy, the
 * settings they constrain, and the posture of each control. Separate from the baselines Baluarte
 * applies to GitLab. Reading needs ``compliance:read``; changes need ``settings:manage``.
 */

import { ApiError, getJson } from "./client";
import { changeFailure, detailText, postJson, sendJson } from "./send";

export type FrameworkId = "soc2" | "iso27001" | "nist_800_53_moderate";
export type SettingSource = "deployment" | "database" | "default";
export type Op = ">=" | "<=" | "==";

export interface Requirement {
  op: Op;
  value: number | boolean;
}

export interface ComplianceSettings {
  audit_retention_days: number;
  evidence_retention_days: number;
  job_retention_days: number;
  session_idle_minutes: number;
  session_lifetime_minutes: number;
  require_review: boolean;
  legal_hold: boolean;
  backup_interval_hours: number;
}

export type SettingName = keyof ComplianceSettings;

export interface Profile {
  frameworks: FrameworkId[];
  /** False until someone chooses a profile, even "none of these". */
  configured: boolean;
  settings: ComplianceSettings;
  sources: Record<SettingName, SettingSource>;
  /** Only the settings the selected frameworks constrain. */
  minimums: Partial<Record<SettingName, Requirement & { frameworks: FrameworkId[] }>>;
  updated_by: string | null;
  updated_at: string | null;
}

export interface ProfileUpdate {
  frameworks: FrameworkId[];
  settings: ComplianceSettings;
}

export type ControlKind = "setting" | "check" | "attestation";

export interface CatalogueControl {
  id: string;
  title: string;
  area: string;
  kind: ControlKind;
  description: string;
  how_to: string;
  mappings: Partial<Record<FrameworkId, string[]>>;
  setting: SettingName | null;
  requirements: Partial<Record<FrameworkId, Requirement>> | null;
  attestation_days: number | null;
}

export interface Catalogue {
  frameworks: { id: FrameworkId; name: string }[];
  controls: CatalogueControl[];
}

export type ControlStatus = "met" | "not_met" | "needs_attestation" | "excepted" | "not_applicable";

export interface ControlPosture {
  id: string;
  title: string;
  status: ControlStatus;
  detail: string;
  evidence: Record<string, unknown>;
  exception: { id: number; expires_on: string } | null;
  attestation: {
    id: number;
    attested_by: string;
    attested_at: string;
    expires_at: string;
  } | null;
}

export interface Posture {
  evaluated_at: string;
  frameworks: FrameworkId[];
  configured: boolean;
  summary: Record<ControlStatus, number>;
  controls: ControlPosture[];
}

export type ExceptionState = "proposed" | "approved" | "rejected" | "revoked";

export interface ControlException {
  id: number;
  control_id: string;
  justification: string;
  expires_on: string;
  state: ExceptionState;
  in_force: boolean;
  requested_by: string;
  requested_at: string;
  decided_by: string | null;
  decided_at: string | null;
  self_approved: boolean;
  note: string | null;
}

export interface Attestation {
  id: number;
  control_id: string;
  statement: string;
  attested_by: string;
  attested_at: string;
  expires_at: string;
}

export interface Violation {
  control_id: string;
  setting: SettingName;
  value: number | boolean;
  required: Requirement;
  frameworks: FrameworkId[];
}

/** A profile change refused because values miss a framework minimum with no exception in force. */
export class BelowMinimum extends ApiError {
  readonly violations: Violation[];

  constructor(violations: Violation[]) {
    super(409, "Some values do not meet the profile's minimums.");
    this.name = "BelowMinimum";
    this.violations = violations;
  }
}

/** The setup wizard and the signed-in app use the same body at different paths. */
export type ProfilePath = "/api/v1/setup/compliance" | "/api/v1/compliance/profile";
export const SETUP_PROFILE: ProfilePath = "/api/v1/setup/compliance";
export const PROFILE: ProfilePath = "/api/v1/compliance/profile";
const BASE = "/api/v1/compliance";

export function getProfile(path: ProfilePath, signal?: AbortSignal): Promise<Profile> {
  return getJson(path, undefined, signal);
}

/** Save the profile. A 409 below_minimum rejects with ``BelowMinimum`` carrying each violation. */
export async function saveProfile(
  path: ProfilePath,
  update: ProfileUpdate,
  csrfToken: string,
): Promise<Profile> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(update),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (response.ok) return (await response.json()) as Profile;
  let detail: unknown = null;
  try {
    detail = ((await response.json()) as { detail?: unknown }).detail;
  } catch {
    // No JSON body: fall through to the generic message.
  }
  if (response.status === 409 && detail && typeof detail === "object" && "code" in detail) {
    const coded = detail as {
      code: string;
      violations?: Violation[];
      settings?: string[];
    };
    if (coded.code === "below_minimum") throw new BelowMinimum(coded.violations ?? []);
    if (coded.code === "deployment_setting") {
      throw new ApiError(
        409,
        `These settings are fixed by the deployment and cannot be changed here: ${(coded.settings ?? []).join(", ")}.`,
      );
    }
  }
  throw new ApiError(response.status, changeFailure(response.status, detailText(detail)));
}

export function getCatalogue(signal?: AbortSignal): Promise<Catalogue> {
  return getJson(`${BASE}/catalogue`, undefined, signal);
}

export function getPosture(signal?: AbortSignal): Promise<Posture> {
  return getJson(`${BASE}/posture`, undefined, signal);
}

export function listControlExceptions(signal?: AbortSignal): Promise<ControlException[]> {
  return getJson(`${BASE}/exceptions`, undefined, signal);
}

export function requestControlException(
  request: { control_id: string; justification: string; expires_on: string },
  csrfToken: string,
): Promise<ControlException> {
  return postJson(`${BASE}/exceptions`, request, csrfToken);
}

export async function decideControlException(
  id: number,
  decision: "approve" | "reject" | "revoke",
  note: string | null,
  csrfToken: string,
): Promise<void> {
  await sendJson(
    "POST",
    `${BASE}/exceptions/${String(id)}/${decision}`,
    decision === "revoke" ? {} : { note },
    csrfToken,
  );
}

export function listAttestations(controlId: string, signal?: AbortSignal): Promise<Attestation[]> {
  return getJson(`${BASE}/attestations`, { control_id: controlId }, signal);
}

export function attest(
  request: { control_id: string; statement: string },
  csrfToken: string,
): Promise<Attestation> {
  return postJson(`${BASE}/attestations`, request, csrfToken);
}
