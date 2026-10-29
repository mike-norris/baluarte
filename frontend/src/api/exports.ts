import { ApiError } from "./client";
import { exportFilename, type ExportRequest } from "../evidence/scope";

export interface ExportDownload {
  blob: Blob;
  filename: string;
  bytes: number;
  /** The ID that names the export's audit events, from ``X-Baluarte-Export-Id``. */
  exportId: string | null;
}

interface ValidationIssue {
  msg?: unknown;
}

function detailText(detail: unknown): string | null {
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

async function failure(response: Response): Promise<string> {
  let detail: string | null = null;
  try {
    const body = (await response.json()) as { detail?: unknown };
    detail = detailText(body.detail);
  } catch {
    detail = null;
  }
  switch (response.status) {
    case 401:
      return "Your session has ended. Sign in again to continue.";
    case 403:
      return detail?.includes("CSRF")
        ? "The request's security token was rejected. Reload the page and try again."
        : "You do not have permission to export evidence.";
    case 409:
      return `Nothing can be exported yet: ${detail ?? "GitLab data is not available"}.`;
    case 422:
      return `The export was refused: ${detail ?? "the scope is not valid"}`;
    default:
      return `The API returned an error (${String(response.status)}).`;
  }
}

/**
 * Request an export and read it to the end. ``onProgress`` receives the bytes read so far.
 * Nothing is returned until every byte has arrived, so a partial export is never offered.
 */
export async function downloadExport(
  request: ExportRequest,
  csrfToken: string,
  signal: AbortSignal,
  onProgress: (bytes: number) => void,
): Promise<ExportDownload> {
  let response: Response;
  try {
    response = await fetch("/api/v1/evidence/exports", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: request.format === "json" ? "application/json" : "application/zip",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(request),
      signal,
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) throw new ApiError(response.status, await failure(response));

  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let bytes = 0;
  try {
    const reader = response.body?.getReader();
    if (reader) {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(new Uint8Array(value));
        bytes += value.byteLength;
        onProgress(bytes);
      }
    } else {
      const whole = new Uint8Array(await response.arrayBuffer());
      chunks.push(whole);
      bytes = whole.byteLength;
      onProgress(bytes);
    }
  } catch (error) {
    if (signal.aborted) throw error;
    throw new ApiError(0, "The download stopped before it finished. Nothing was saved.");
  }
  return {
    blob: new Blob(chunks, {
      type: response.headers.get("Content-Type") ?? "",
    }),
    filename: exportFilename(response.headers.get("Content-Disposition"), request.format),
    bytes,
    exportId: response.headers.get("X-Baluarte-Export-Id"),
  };
}

/** Hand a finished download to the browser under ``filename``. */
export function saveDownload(download: ExportDownload): void {
  const url = URL.createObjectURL(download.blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = download.filename;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  // Revoke after the click has been handled, so the browser has started the save.
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}
