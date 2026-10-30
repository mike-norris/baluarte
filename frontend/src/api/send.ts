/** Requests that change state: JSON bodies, the CSRF header, and the API's reason on failure. */

import { ApiError } from "./client";

interface ValidationIssue {
  msg?: unknown;
}

/** The API's ``detail``: a message, or FastAPI's list of validation issues joined into one. */
export function detailText(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = (detail as ValidationIssue[])
      .map((issue) => (typeof issue.msg === "string" ? issue.msg : ""))
      .map((message) => message.replace(/^Value error, /, ""))
      .filter(Boolean);
    return messages.length > 0 ? messages.join(" ") : null;
  }
  return null;
}

/** The ``detail`` of a failed response, or null when it has none. */
export async function responseDetail(response: Response): Promise<string | null> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return detailText(body.detail);
  } catch {
    return null;
  }
}

/** A sentence for a failed change; the API's own reason is kept where it explains the refusal. */
export function changeFailure(status: number, detail: string | null): string {
  switch (status) {
    case 401:
      return "Your session has ended. Sign in again to continue.";
    case 403:
      if (detail?.includes("CSRF") || detail?.includes("Cross-origin")) {
        return "The request's security token was rejected. Reload the page and try again.";
      }
      // Separation of duties is a 403 with its own explanation.
      return detail && detail !== "Insufficient permissions"
        ? `Refused: ${detail}.`
        : "You do not have permission to do this.";
    case 404:
      return `Not found: ${detail ?? "it no longer exists"}.`;
    case 409:
      return `Not possible now: ${detail ?? "the record changed"}.`;
    case 422:
      return `Refused: ${detail ?? "the request is not valid"}`;
    default:
      return `The API returned an error (${String(status)}).`;
  }
}

/** POST ``body`` as JSON with the session's CSRF token; resolves to the JSON response. */
export async function postJson<T>(path: string, body: unknown, csrfToken: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      changeFailure(response.status, await responseDetail(response)),
    );
  }
  return (await response.json()) as T;
}
