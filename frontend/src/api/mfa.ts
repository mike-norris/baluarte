/** Built-in TOTP: the sign-in step that asks for a code, and administrators' resets. */

import { getJson } from "./client";
import { postJson, sendJson } from "./send";

const MFA = "/api/v1/auth/mfa";
const ENROLLMENTS = "/api/v1/settings/mfa/enrollments";

/** A sign-in that has passed the identity provider and waits for a code. */
export interface MfaStatus {
  enrolled: boolean;
  account: string;
  csrf_token: string;
  max_failures: number;
  failures: number;
}

export interface MfaEnrollment {
  /** Base32, for typing into an app that cannot scan. */
  secret: string;
  uri: string;
  /** Rows of "1" (dark) and "0" (light), without the quiet zone. */
  qr: string[];
}

export interface MfaVerified {
  return_to: string;
  method: "totp" | "recovery";
  /** Shown once, when a new authenticator was confirmed. */
  recovery_codes: string[];
  recovery_codes_left: number | null;
}

export interface MfaEnrollmentRow {
  user_id: number;
  email: string | null;
  display_name: string | null;
  confirmed_at: string;
  recovery_codes_left: number;
}

export function getMfaStatus(signal?: AbortSignal): Promise<MfaStatus> {
  return getJson<MfaStatus>(MFA, undefined, signal);
}

export function enrollTotp(csrf: string): Promise<MfaEnrollment> {
  return postJson<MfaEnrollment>(`${MFA}/enroll`, {}, csrf);
}

export function verifyTotp(code: string, returnTo: string, csrf: string): Promise<MfaVerified> {
  return postJson<MfaVerified>(`${MFA}/verify`, { code, return_to: returnTo }, csrf);
}

export function listMfaEnrollments(signal?: AbortSignal): Promise<MfaEnrollmentRow[]> {
  return getJson<MfaEnrollmentRow[]>(ENROLLMENTS, undefined, signal);
}

export async function resetMfaEnrollment(userId: number, csrf: string): Promise<void> {
  await sendJson<null>("DELETE", `${ENROLLMENTS}/${String(userId)}`, undefined, csrf);
}
