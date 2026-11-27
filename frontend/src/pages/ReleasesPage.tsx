import { useRef, useState } from "react";

import { saveDownload } from "../api/exports";
import { downloadAuditRelease, listReleases, type Release } from "../api/releases";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { useCanExport } from "./RegisterExport";
import { asApiError, Problems } from "../settings/parts";
import { formatTime } from "../time";
import { useCsrf } from "../templates/format";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

/** Who started, approved and deployed a release, as GitLab records them. */
export function peopleText(release: Release): string {
  const approvers =
    release.approvers === null
      ? "approvals not read"
      : release.approvers.length === 0
        ? "no approvals"
        : `approved by ${release.approvers.join(", ")}`;
  return [
    `started by ${release.started_by ?? "not known"}`,
    approvers,
    `deployed by ${release.deployer ?? "not known"}`,
  ].join("; ");
}

function refText(release: Release): string {
  const kind = release.tag === true ? "tag" : release.tag === false ? "branch" : "ref";
  return `${kind} ${release.ref ?? "not known"}`;
}

export function ReleasesPage() {
  const { token } = useCsrf();
  const canExport = useCanExport();
  const loaded = useLoad("releases", (signal) => listReleases(signal));
  const [includeReports, setIncludeReports] = useState(true);
  const [busy, setBusy] = useState<number | null>(null);
  const [saved, setSaved] = useState<{ filename: string; exportId: string | null } | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const abort = useRef<AbortController | null>(null);

  async function exportRelease(release: Release) {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(release.change_id);
    setSaved(null);
    setProblems([]);
    try {
      const result = await downloadAuditRelease(
        release.change_id,
        includeReports,
        token ?? "",
        controller.signal,
      );
      saveDownload(result);
      setSaved({ filename: result.filename, exportId: result.exportId });
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<Release>[] = [
    {
      key: "project",
      header: "Project",
      rowHeader: true,
      priority: 1,
      render: (release) => (
        <>
          <span className="fw-medium">{release.project_path}</span>
          <div className="text-secondary small">
            {release.release_name ? `${release.release_name}, ` : ""}
            {refText(release)}
          </div>
        </>
      ),
    },
    {
      key: "finished",
      header: "Deployed",
      priority: 2,
      render: (release) => (
        <>
          {formatTime(release.finished_at)}
          <div className="text-secondary small">
            {release.environment ?? "environment not known"}
          </div>
        </>
      ),
    },
    {
      key: "people",
      header: "Who",
      priority: 1,
      render: (release) => peopleText(release),
    },
  ];
  if (canExport) {
    columns.push({
      key: "export",
      header: "Audit release",
      priority: 1,
      render: (release) => (
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy !== null}
          onClick={() => void exportRelease(release)}
        >
          {busy === release.change_id ? "Building…" : "Export audit release"}
        </button>
      ),
    });
  }

  const releases = loaded.data ?? [];
  return (
    <section aria-labelledby="page-title" className="stack-4">
      <GovernanceHeader intro="Recorded production releases, with who started, approved and deployed each as GitLab records them. An audit release packages one release with its evidence for an auditor: never source code, verifiable offline." />
      {canExport && (
        <div className="form-check">
          <input
            id="include-reports"
            type="checkbox"
            className="form-check-input"
            checked={includeReports}
            onChange={(event) => {
              setIncludeReports(event.target.checked);
            }}
          />
          <label htmlFor="include-reports" className="form-check-label">
            Include the release pipeline&apos;s reports (tests, SBOM, security scans), read from
            GitLab, at most 10 MB each and 50 MB per release
          </label>
        </div>
      )}
      {saved && (
        <Notice tone="success" role="status">
          Downloaded {saved.filename}. Check it with{" "}
          <code>baluarte-verify-export {saved.filename} --manifest-sha256 &lt;value&gt;</code>,
          using the manifest hash recorded for export {saved.exportId ?? "(unknown)"} in the audit
          log (<code>release.export_completed</code>).
        </Notice>
      )}
      <Problems messages={problems} />
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {!loaded.data && !loaded.error && <Loading rows={4} />}
      {loaded.data && releases.length === 0 && (
        <EmptyState title="No production releases are recorded yet" />
      )}
      {releases.length > 0 && (
        <div className="card">
          <ResponsiveTable
            columns={columns}
            rows={releases}
            rowKey={(release) => release.change_id}
            caption="Production releases"
          />
        </div>
      )}
    </section>
  );
}
