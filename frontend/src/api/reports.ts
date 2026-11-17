/**
 * The auditor report: a PDF of one framework's controls over an audit period, read as the
 * evidence package reads it (docs/AUDIT_EVIDENCE.md#auditor-report).
 */

import type { FrameworkId } from "./controls";
import { exportFilename } from "../evidence/scope";
import { streamPost, type ExportDownload } from "./exports";

export const REPORT_PERMISSION = "evidence:export";

export interface ReportRequest {
  framework: FrameworkId;
  /** UTC instants; the period is ``from`` up to, not including, ``to``. */
  from: string;
  to: string;
  /** Group full paths; every project in them and their subgroups. */
  groups?: string[];
}

/** Request the report and read it to the end; nothing is returned for a partial PDF. */
export async function downloadReport(
  request: ReportRequest,
  csrfToken: string,
  signal: AbortSignal,
  onProgress: (bytes: number) => void,
): Promise<ExportDownload> {
  const { response, chunks, bytes } = await streamPost(
    "/api/v1/evidence/reports",
    request,
    "application/pdf",
    csrfToken,
    signal,
    onProgress,
  );
  return {
    blob: new Blob(chunks, { type: response.headers.get("Content-Type") ?? "application/pdf" }),
    filename: exportFilename(response.headers.get("Content-Disposition"), "pdf"),
    bytes,
    exportId: response.headers.get("X-Baluarte-Report-Id"),
  };
}
