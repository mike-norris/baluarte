/**
 * Auditor evidence packages: everything for a period, with a reproducible sample of changes
 * (docs/AUDIT_EVIDENCE.md). The same seed over the same period draws the same sample.
 */

import { getJson } from "./client";
import type { FrameworkId } from "./controls";
import { exportFilename } from "../evidence/scope";
import { streamPost, type ExportDownload } from "./exports";

export const PACKAGE_PERMISSION = "evidence:package";
export const MAX_SAMPLE = 500;

export interface PackageRequest {
  /** UTC instants; the period is ``from`` up to, not including, ``to``. */
  from: string;
  to: string;
  framework?: FrameworkId;
  control_id?: string;
  /** Group full paths; every project in them and their subgroups. */
  groups?: string[];
  sample: { size: number; seed?: number };
}

export interface PackageDownload extends ExportDownload {
  /** The seed the sample was drawn with, from ``X-Baluarte-Sample-Seed``. */
  seed: string | null;
}

/** A package as recorded; ``manifest_sha256`` is null until the download has finished. */
export interface PackageRecord {
  export_id: string;
  seed: string | number | null;
  manifest_sha256: string | null;
  sample_size?: number;
  from?: string;
  to?: string;
}

export async function downloadPackage(
  request: PackageRequest,
  csrfToken: string,
  signal: AbortSignal,
  onProgress: (bytes: number) => void,
): Promise<PackageDownload> {
  const { response, chunks, bytes } = await streamPost(
    "/api/v1/evidence/packages",
    request,
    "application/zip",
    csrfToken,
    signal,
    onProgress,
  );
  return {
    blob: new Blob(chunks, { type: response.headers.get("Content-Type") ?? "" }),
    filename: exportFilename(response.headers.get("Content-Disposition"), "csv"),
    bytes,
    exportId: response.headers.get("X-Baluarte-Export-Id"),
    seed: response.headers.get("X-Baluarte-Sample-Seed"),
  };
}

export function getPackage(exportId: string, signal?: AbortSignal): Promise<PackageRecord> {
  return getJson(`/api/v1/evidence/packages/${encodeURIComponent(exportId)}`, undefined, signal);
}
