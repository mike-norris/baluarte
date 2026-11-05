import { ApiError } from "./client";
import { detailMessage, readDetail, reasonSentences, RefusalError } from "./send";
import { exportFilename, type ExportRequest } from "../evidence/scope";

export interface ExportDownload {
  blob: Blob;
  filename: string;
  bytes: number;
  /** The ID that names the export's audit events, from ``X-Baluarte-Export-Id``. */
  exportId: string | null;
}

async function failure(response: Response): Promise<ApiError> {
  const body = await readDetail(response);
  const reasons = reasonSentences(body);
  const message = sentence(response.status, detailMessage(body));
  return reasons.length > 0
    ? new RefusalError(response.status, message, reasons)
    : new ApiError(response.status, message);
}

function sentence(status: number, detail: string | null): string {
  switch (status) {
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
      return `The API returned an error (${String(status)}).`;
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
  const { response, chunks, bytes } = await streamPost(
    "/api/v1/evidence/exports",
    request,
    request.format === "json" ? "application/json" : "application/zip",
    csrfToken,
    signal,
    onProgress,
  );
  return {
    blob: new Blob(chunks, {
      type: response.headers.get("Content-Type") ?? "",
    }),
    filename: exportFilename(response.headers.get("Content-Disposition"), request.format),
    bytes,
    exportId: response.headers.get("X-Baluarte-Export-Id"),
  };
}

/**
 * POST ``body`` and read the whole response, reporting bytes as they arrive. A failure carries
 * the export failure sentences; an interrupted body saves nothing.
 */
export async function streamPost(
  path: string,
  body: unknown,
  accept: string,
  csrfToken: string,
  signal: AbortSignal,
  onProgress: (bytes: number) => void,
): Promise<{ response: Response; chunks: Uint8Array<ArrayBuffer>[]; bytes: number }> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: accept,
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new ApiError(0, "The API could not be reached.");
  }
  if (!response.ok) throw await failure(response);

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
  return { response, chunks, bytes };
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
