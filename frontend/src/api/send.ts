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

/** The body's ``detail`` as sent, or null when the body is not JSON. */
export async function readDetail(response: Response): Promise<unknown> {
  try {
    return ((await response.json()) as { detail?: unknown }).detail ?? null;
  } catch {
    return null;
  }
}

/** A plain or validation ``detail``, or the ``message`` of a structured one. */
export function detailMessage(detail: unknown): string | null {
  if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
    const message = (detail as { message?: unknown }).message;
    return typeof message === "string" ? message : null;
  }
  return detailText(detail);
}

/**
 * The ``reasons`` of a structured refusal (``{code, message, reasons}``) as sentences: a code is
 * read with ``words``, or as words; a ``{code, message}`` item by its message.
 */
export function reasonSentences(detail: unknown, words: Record<string, string> = {}): string[] {
  if (typeof detail !== "object" || detail === null || Array.isArray(detail)) return [];
  const reasons = (detail as { reasons?: unknown }).reasons;
  if (!Array.isArray(reasons)) return [];
  // Codes read as words; anything else, such as a path, is kept as sent.
  const sentence = (code: string) =>
    words[code] ?? (/^[a-z]+(_[a-z]+)+$/.test(code) ? code.replace(/_/g, " ") : code);
  return reasons
    .map((item: unknown) => {
      if (typeof item === "string") return sentence(item);
      if (typeof item !== "object" || item === null) return null;
      const record = item as { code?: unknown; message?: unknown };
      if (typeof record.message === "string") return record.message;
      return typeof record.code === "string" ? sentence(record.code) : null;
    })
    .filter((text): text is string => text !== null);
}

/** A refused request, with the API's reasons as sentences. */
export class RefusalError extends ApiError {
  readonly reasons: string[];

  constructor(status: number, message: string, reasons: string[]) {
    super(status, message);
    this.name = "RefusalError";
    this.reasons = reasons;
  }
}

/** The reasons an error carries; empty for any other error. */
export function errorReasons(error: unknown): string[] {
  return error instanceof RefusalError ? error.reasons : [];
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
    case 429:
      return "Too many attempts. Wait a few minutes and try again.";
    default:
      return `The API returned an error (${String(status)}).`;
  }
}

type Method = "POST" | "PUT" | "PATCH" | "DELETE";

/** Send ``body`` as JSON with a CSRF token; resolves to the JSON response, or null for 204. */
export async function sendJson<T>(
  method: Method,
  path: string,
  body: unknown,
  csrfToken: string,
  words: Record<string, string> = {},
): Promise<T | null> {
  const { data } = await sendJsonWithStatus<T>(method, path, body, csrfToken, words);
  return data;
}

/** As ``sendJson``, keeping the success status for endpoints where 200 and 201 differ. */
export async function sendJsonWithStatus<T>(
  method: Method,
  path: string,
  body: unknown,
  csrfToken: string,
  words: Record<string, string> = {},
): Promise<{ status: number; data: T | null }> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) {
    const detail = await readDetail(response);
    const message = changeFailure(response.status, detailMessage(detail));
    const reasons = reasonSentences(detail, words);
    throw reasons.length > 0
      ? new RefusalError(response.status, message, reasons)
      : new ApiError(response.status, message);
  }
  if (response.status === 204) return { status: 204, data: null };
  return { status: response.status, data: (await response.json()) as T };
}

/** POST ``body`` as JSON with the session's CSRF token; resolves to the JSON response. */
export async function postJson<T>(path: string, body: unknown, csrfToken: string): Promise<T> {
  const result = await sendJson<T>("POST", path, body, csrfToken);
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}
