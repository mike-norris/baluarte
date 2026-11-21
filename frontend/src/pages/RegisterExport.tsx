import { useRef, useState } from "react";

import { saveDownload } from "../api/exports";
import {
  downloadRegisterExport,
  EXPORT_PERMISSION,
  type RegisterExportRequest,
} from "../api/register";
import { useAuth } from "../auth/context";
import { asApiError, Problems } from "../settings/parts";
import { errorReasons } from "../api/send";
import { Notice } from "../ui/Notice";

export function useCanExport(): boolean {
  const { session } = useAuth();
  return session.status === "signed_in" && session.user.permissions.includes(EXPORT_PERMISSION);
}

/**
 * Builds a register export package and saves it, then says how to verify it. Shown only to
 * those who may build packages; ``request`` null disables it (nothing chosen yet).
 */
export function ExportButton({
  request,
  label,
}: {
  request: RegisterExportRequest | null;
  label: string;
}) {
  const { session } = useAuth();
  const csrf = session.status === "signed_in" ? session.user.csrf_token : "";
  const canExport = useCanExport();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<{
    filename: string;
    exportId: string | null;
  } | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const abort = useRef<AbortController | null>(null);
  if (!canExport) return null;

  async function run() {
    if (request === null) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setSaved(null);
    setProblems([]);
    try {
      const result = await downloadRegisterExport(request, csrf, controller.signal);
      saveDownload(result);
      setSaved({ filename: result.filename, exportId: result.exportId });
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack-2">
      <div>
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy || request === null}
          onClick={() => void run()}
        >
          {busy ? "Building the package…" : label}
        </button>
      </div>
      {saved && (
        <Notice tone="success" role="status">
          Downloaded {saved.filename}. Check it with{" "}
          <code>baluarte-verify-export {saved.filename} --manifest-sha256 &lt;value&gt;</code>,
          using the manifest hash recorded for export {saved.exportId ?? "(unknown)"} in the audit
          log (<code>register.export_completed</code>).
        </Notice>
      )}
      <Problems messages={problems} />
    </div>
  );
}
