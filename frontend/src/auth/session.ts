/** Client for the API's session endpoints. The server enforces access; this only reflects it. */

export interface CurrentUser {
  /** The internal ID the audit log and exceptions name; the signed-in user's own. */
  user_id: number;
  subject: string;
  email: string | null;
  display_name: string | null;
  roles: string[];
  permissions: string[];
  session_expires_at: string;
  csrf_token: string;
}

export type Session =
  | { status: "loading" }
  | { status: "signed_in"; user: CurrentUser }
  | { status: "signed_out" }
  | { status: "not_configured" }
  | { status: "error" };

export async function fetchSession(signal?: AbortSignal): Promise<Session> {
  try {
    const response = await fetch("/api/v1/auth/me", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: signal ?? null,
    });
    if (response.ok) {
      return { status: "signed_in", user: (await response.json()) as CurrentUser };
    }
    if (response.status === 401) return { status: "signed_out" };
    if (response.status === 503) return { status: "not_configured" };
    return { status: "error" };
  } catch (error) {
    if (signal?.aborted) throw error;
    return { status: "error" };
  }
}

export async function signOut(csrfToken: string): Promise<void> {
  const response = await fetch("/api/v1/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken },
  });
  // 401 means the session had already ended, which is the outcome we wanted.
  if (!response.ok && response.status !== 401) {
    throw new Error(`Sign-out failed (${String(response.status)})`);
  }
}

/** Full-page navigation to the API, which redirects to the identity provider. */
export function signInHref(returnTo: string): string {
  const path = returnTo.startsWith("/sign-in") ? "/" : returnTo;
  return `/api/v1/auth/login?${new URLSearchParams({ return_to: path }).toString()}`;
}

export function displayName(user: CurrentUser): string {
  return user.display_name ?? user.email ?? user.subject;
}
