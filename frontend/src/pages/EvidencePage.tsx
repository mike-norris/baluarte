import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import { frameworkIds } from "../api/controls";
import { outcomes, type FindingState, type Outcome } from "../api/evaluations";
import { downloadExport, saveDownload } from "../api/exports";
import { useAuth } from "../auth/context";
import {
  findingStates,
  readExportForm,
  validateExport,
  writeExportParams,
  type ExportForm,
  type ExportFormat,
} from "../evidence/scope";
import { frameworkLabels } from "../controls/format";
import { findingStateLabels, outcomeLabels } from "../findings/format";
import { PACKAGE_PERMISSION } from "../api/packages";
import { PackageForm } from "../evidence/PackageForm";
import { CollectionNote } from "../freshness/CollectionNote";
import { PageHeader } from "../ui/PageHeader";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";

export const EXPORT_PERMISSION = "evidence:export";

type Download =
  | { status: "idle" }
  | { status: "running"; bytes: number }
  | { status: "done"; filename: string; bytes: number; exportId: string | null }
  | { status: "cancelled" }
  | { status: "failed"; error: ApiError };

const code = "";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

/** The form's values; disabled fields are not submitted, so "everything" ignores them. */
function formValues(form: FormData): ExportForm {
  return {
    all: form.get("all") === "on",
    group: text(form, "group"),
    projects: text(form, "projects"),
    policy: text(form, "policy"),
    rule: text(form, "rule"),
    outcome: outcomes.find((item) => item === text(form, "outcome")) ?? "",
    findingState: findingStates.find((item) => item === text(form, "finding_state")) ?? "",
    since: text(form, "since"),
    until: text(form, "until"),
    framework: frameworkIds.find((item) => item === text(form, "framework")) ?? "",
    control: text(form, "control"),
    format: text(form, "format") === "csv" ? "csv" : "json",
  };
}

function ScopeFields({ initial, disabled }: { initial: ExportForm; disabled: boolean }) {
  return (
    <fieldset disabled={disabled} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <legend className="visually-hidden">Scope</legend>
      <div>
        <label className="fw-medium">
          Group path
          <input
            name="group"
            defaultValue={initial.group}
            maxLength={255}
            placeholder="platform/backend"
            aria-describedby="group-help"
            className="form-control mt-1"
          />
        </label>
        <p id="group-help" className="mt-1 small text-secondary">
          Projects in this group and all its subgroups.
        </p>
      </div>
      <div>
        <label className="fw-medium">
          Project GitLab IDs
          <input
            name="projects"
            defaultValue={initial.projects}
            inputMode="numeric"
            placeholder="105, 230"
            aria-describedby="projects-help"
            className="form-control mt-1"
          />
        </label>
        <p id="projects-help" className="mt-1 small text-secondary">
          Separated by commas; at most 500.
        </p>
      </div>
      <label className="fw-medium">
        Policy name
        <input
          name="policy"
          defaultValue={initial.policy}
          maxLength={63}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        Rule ID
        <input
          name="rule"
          defaultValue={initial.rule}
          maxLength={63}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        Outcome
        <select name="outcome" defaultValue={initial.outcome} className="form-select mt-1">
          <option value="">Any</option>
          {outcomes.map((outcome: Outcome) => (
            <option key={outcome} value={outcome}>
              {outcomeLabels[outcome]}
            </option>
          ))}
        </select>
      </label>
      <label className="fw-medium">
        Finding state
        <select
          name="finding_state"
          defaultValue={initial.findingState}
          className="form-select mt-1"
        >
          <option value="">Any</option>
          {findingStates.map((state: FindingState) => (
            <option key={state} value={state}>
              {findingStateLabels[state]}
            </option>
          ))}
        </select>
      </label>
      <label className="fw-medium">
        Evaluated from (UTC)
        <input
          name="since"
          type="datetime-local"
          defaultValue={initial.since}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        Evaluated before (UTC)
        <input
          name="until"
          type="datetime-local"
          defaultValue={initial.until}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        Framework
        <select name="framework" defaultValue={initial.framework} className="form-select mt-1">
          <option value="">Any</option>
          {frameworkIds.map((framework) => (
            <option key={framework} value={framework}>
              {frameworkLabels[framework]}
            </option>
          ))}
        </select>
      </label>
      <div>
        <label className="fw-medium">
          Control ID
          <input
            name="control"
            defaultValue={initial.control}
            maxLength={20}
            placeholder="CC8.1"
            aria-describedby="control-help"
            className="form-control mt-1"
          />
        </label>
        <p id="control-help" className="mt-1 small text-secondary">
          With a framework: findings of the rules mapped to that control.
        </p>
      </div>
    </fieldset>
  );
}

function ExportFormFields({
  initial,
  busy,
  onSubmit,
}: {
  initial: ExportForm;
  busy: boolean;
  onSubmit: (form: ExportForm) => void;
}) {
  const [all, setAll] = useState(initial.all);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit(formValues(new FormData(event.currentTarget)));
  }

  return (
    <form onSubmit={submit} aria-label="Export evidence" className="card card-body mb-3">
      <label className="d-flex align-items-start gap-2 fw-medium">
        <input
          name="all"
          type="checkbox"
          checked={all}
          onChange={(event) => {
            setAll(event.currentTarget.checked);
          }}
          className="mt-0.5"
        />
        <span>
          Export everything
          <span className="d-block small fw-normal text-secondary">
            Every project, policy and rule. Clears the scope below, which is otherwise required.
          </span>
        </span>
      </label>
      <ScopeFields initial={initial} disabled={all} />
      <fieldset>
        <legend className="fw-medium">Format</legend>
        <div className="mt-1 d-flex flex-wrap gap-3">
          {(
            [
              ["json", "JSON (one document)"],
              ["csv", "ZIP of CSV files"],
            ] as [ExportFormat, string][]
          ).map(([value, label]) => (
            <label key={value} className="d-flex align-items-center gap-2">
              <input
                type="radio"
                name="format"
                value={value}
                defaultChecked={initial.format === value}
              />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      <button type="submit" disabled={busy} className="btn btn-primary">
        Export
      </button>
    </form>
  );
}

function Verification({ done }: { done: Extract<Download, { status: "done" }> | null }) {
  const file = done?.filename ?? "<file>";
  return (
    <section aria-labelledby="verify-title" className="text-secondary">
      <h2 id="verify-title" className="fs-3 fw-semibold">
        Verifying an export
      </h2>
      <p>
        Every export ends with a manifest listing each file’s or section’s SHA-256 and row count,
        the policy versions, observation digest results and collection errors. A ZIP also holds{" "}
        <code className={code}>SHA256SUMS</code>, so{" "}
        <code className={code}>sha256sum -c SHA256SUMS</code> checks its extracted files with
        standard tools.
      </p>
      <p>
        A package cannot hold its own hash, so the manifest’s SHA-256 is recorded only in the audit
        log: <code className={code}>manifest_sha256</code> in the{" "}
        <code className={code}>evidence.export_completed</code> event
        {done?.exportId ? (
          <>
            {" "}
            for export <code className={code}>{done.exportId}</code>
          </>
        ) : (
          " named by the export ID"
        )}
        . Admins and auditors can read it from{" "}
        <code className={code}>GET /api/v1/audit-events?action=evidence.export_completed</code>;
        prefer the copy in the <code className={code}>baluarte.audit</code> log stream kept outside
        the database.
      </p>
      <p>Then check the package offline, with no database:</p>
      <pre className="small mb-0">
        {`baluarte-verify-export ${file} --manifest-sha256 <manifest_sha256>`}
      </pre>
      <p>
        It exits 0 when every check passes, 1 when any fails and 2 when the file cannot be read.
        Evidence that was already invalid when exported is reported as a warning, not hidden.
      </p>
    </section>
  );
}

function Progress({ download, cancel }: { download: Download; cancel: () => void }) {
  if (download.status === "running") {
    return (
      <div className="rounded border p-3">
        <p role="status">Exporting… The file is offered once every byte has arrived.</p>
        <p className="text-secondary">{`${formatBytes(download.bytes)} received`}</p>
        <button type="button" onClick={cancel} className="btn">
          Cancel export
        </button>
      </div>
    );
  }
  if (download.status === "done") {
    return (
      <p role="status" className="alert alert-success text-body">
        {`Downloaded ${download.filename} (${formatBytes(download.bytes)}). Verify it before relying on it.`}
      </p>
    );
  }
  if (download.status === "cancelled") {
    return (
      <p role="status" className="text-secondary">
        Export cancelled. Nothing was saved.
      </p>
    );
  }
  if (download.status === "failed") {
    return (
      <p role="alert" className="alert alert-danger text-body">
        {download.error.message}
      </p>
    );
  }
  return null;
}

function Exporter({ csrfToken }: { csrfToken: string }) {
  const [params] = useSearchParams();
  const { refresh } = useAuth();
  const [errors, setErrors] = useState<string[]>([]);
  const [download, setDownload] = useState<Download>({ status: "idle" });
  const controller = useRef<AbortController | null>(null);

  // Stop a running export if the page goes away.
  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  async function start(form: ExportForm) {
    const checked = validateExport(form);
    if ("errors" in checked) {
      setErrors(checked.errors);
      return;
    }
    setErrors([]);
    const current = new AbortController();
    controller.current = current;
    setDownload({ status: "running", bytes: 0 });
    try {
      const result = await downloadExport(checked.request, csrfToken, current.signal, (bytes) => {
        setDownload({ status: "running", bytes });
      });
      saveDownload(result);
      setDownload({
        status: "done",
        filename: result.filename,
        bytes: result.bytes,
        exportId: result.exportId,
      });
    } catch (error) {
      if (current.signal.aborted) {
        setDownload({ status: "cancelled" });
        return;
      }
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The export could not be read.");
      setDownload({ status: "failed", error: apiError });
      if (apiError.status === 401) void refresh();
    } finally {
      if (controller.current === current) controller.current = null;
    }
  }

  return (
    <div>
      <ExportFormFields
        key={writeExportParams(readExportForm(params)).toString()}
        initial={readExportForm(params)}
        busy={download.status === "running"}
        onSubmit={(form) => void start(form)}
      />
      {errors.length > 0 && (
        <div role="alert" className="alert alert-danger text-body">
          <p className="fw-medium">The export was not started:</p>
          <ul className="mt-1 ps-3">
            {errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      )}
      <Progress
        download={download}
        cancel={() => {
          controller.current?.abort();
        }}
      />
      <Verification done={download.status === "done" ? download : null} />
    </div>
  );
}

export function EvidencePage() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const allowed = user?.permissions.includes(EXPORT_PERMISSION) ?? false;
  const packages = user?.permissions.includes(PACKAGE_PERMISSION) ?? false;
  // Large downloads and file checks are unreliable on phones, so exports need a larger screen.
  const phone = useDeviceClass() === "phone";

  return (
    <section aria-labelledby="page-title">
      <div>
        <PageHeader
          pretitle="Evidence"
          title="Evidence"
          subtitle="Export findings, evaluations and the GitLab observations they judged, with collection errors and freshness. Unknown and failed collection state is exported, never dropped."
        />
      </div>
      <CollectionNote />
      {phone ? (
        <EmptyState title="Evidence exports are made on a larger screen." />
      ) : user && allowed ? (
        <Exporter csrfToken={user.csrf_token} />
      ) : (
        <p className="rounded border p-3 text-secondary">
          Exporting evidence needs the <code className={code}>{EXPORT_PERMISSION}</code> permission,
          which the admin and auditor roles have. Exports are bulk copies of inventory and evidence,
          so ask an administrator if you need one.
        </p>
      )}
      {user && packages && !phone && <PackageForm csrfToken={user.csrf_token} />}
    </section>
  );
}
