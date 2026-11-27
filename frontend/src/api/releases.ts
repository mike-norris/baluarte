/** Production releases and audit release packages (docs/RELEASE_GOVERNANCE.md, RG.4). */

import { exportFilename } from "../evidence/scope";
import { getJson } from "./client";
import { streamPost, type ExportDownload } from "./exports";

export interface Release {
  change_id: number;
  connection_id: number;
  project_id: number;
  project_path: string;
  deployment_id: number | null;
  environment: string | null;
  ref: string | null;
  tag: boolean | null;
  sha: string | null;
  finished_at: string;
  deployer: string | null;
  started_by: string | null;
  /** Who approved it; null when the approvals were not read. */
  approvers: string[] | null;
  release_name: string | null;
}

export function listReleases(signal: AbortSignal, limit = 100): Promise<Release[]> {
  return getJson<Release[]>("/api/v1/releases", { limit }, signal);
}

/** Build the audit release of one production release and read it whole. */
export async function downloadAuditRelease(
  changeId: number,
  includeReports: boolean,
  csrf: string,
  signal: AbortSignal,
): Promise<ExportDownload> {
  const { response, chunks, bytes } = await streamPost(
    "/api/v1/releases/exports",
    { change_id: changeId, include_reports: includeReports },
    "application/zip",
    csrf,
    signal,
    () => undefined,
  );
  return {
    blob: new Blob(chunks, { type: response.headers.get("Content-Type") ?? "" }),
    // A CSV-format export is a ZIP, so this gives the .zip extension.
    filename: exportFilename(response.headers.get("Content-Disposition"), "csv"),
    bytes,
    exportId: response.headers.get("X-Baluarte-Export-Id"),
  };
}
