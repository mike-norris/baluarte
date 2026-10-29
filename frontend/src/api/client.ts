/** Read-only JSON client for the Baluarte API. The server enforces access; errors carry status. */

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

export function withQuery(path: string, params: Params = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

function describe(status: number): string {
  if (status === 401) return "Your session has ended. Sign in again to continue.";
  if (status === 403) return "You do not have permission to view this.";
  if (status === 503) return "Baluarte is not fully configured yet.";
  return `The API returned an error (${String(status)}).`;
}

export async function getJson<T>(path: string, params?: Params, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(withQuery(path, params), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: signal ?? null,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) throw new ApiError(response.status, describe(response.status));
  return (await response.json()) as T;
}
