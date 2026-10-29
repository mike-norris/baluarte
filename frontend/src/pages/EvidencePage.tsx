import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import { outcomes, type FindingState, type Outcome } from "../api/evaluations";
import { downloadExport, saveDownload } from "../api/exports";
import { useAuth } from "../auth/context";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import {
  findingStates,
  readExportForm,
  validateExport,
  writeExportParams,
  type ExportForm,
  type ExportFormat,
} from "../evidence/scope";
import { findingStateLabels, outcomeLabels } from "../findings/format";
import { CollectionNote } from "../freshness/CollectionNote";

export const EXPORT_PERMISSION = "evidence:export";

type Download =
  | { status: "idle" }
  | { status: "running"; bytes: number }
  | { status: "done"; filename: string; bytes: number; exportId: string | null }
  | { status: "cancelled" }
  | { status: "failed"; error: ApiError };

const code = "rounded bg-slate-100 px-1 py-0.5 font-mono text-xs";

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
    format: text(form, "format") === "csv" ? "csv" : "json",
  };
}

function ScopeFields({ initial, disabled }: { initial: ExportForm; disabled: boolean }) {
  return (
    <fieldset disabled={disabled} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <legend className="sr-only">Scope</legend>
      <div>
        <label className="text-sm font-medium">
          Group path
          <input
            name="group"
            defaultValue={initial.group}
            maxLength={255}
            placeholder="platform/backend"
            aria-describedby="group-help"
            className={fieldClass}
          />
        </label>
        <p id="group-help" className="mt-1 text-xs text-slate-600">
          Projects in this group and all its subgroups.
        </p>
      </div>
      <div>
        <label className="text-sm font-medium">
          Project GitLab IDs
          <input
            name="projects"
            defaultValue={initial.projects}
            inputMode="numeric"
            placeholder="105, 230"
            aria-describedby="projects-help"
            className={fieldClass}
          />
        </label>
        <p id="projects-help" className="mt-1 text-xs text-slate-600">
          Separated by commas; at most 500.
        </p>
      </div>
      <label className="text-sm font-medium">
        Policy name
        <input name="policy" defaultValue={initial.policy} maxLength={63} className={fieldClass} />
      </label>
      <label className="text-sm font-medium">
        Rule ID
        <input name="rule" defaultValue={initial.rule} maxLength={63} className={fieldClass} />
      </label>
      <label className="text-sm font-medium">
        Outcome
        <select name="outcome" defaultValue={initial.outcome} className={fieldClass}>
          <option value="">Any</option>
          {outcomes.map((outcome: Outcome) => (
            <option key={outcome} value={outcome}>
              {outcomeLabels[outcome]}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm font-medium">
        Finding state
        <select name="finding_state" defaultValue={initial.findingState} className={fieldClass}>
          <option value="">Any</option>
          {findingStates.map((state: FindingState) => (
            <option key={state} value={state}>
              {findingStateLabels[state]}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm font-medium">
        Evaluated from (UTC)
        <input
          name="since"
          type="datetime-local"
          defaultValue={initial.since}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        Evaluated before (UTC)
        <input
          name="until"
          type="datetime-local"
          defaultValue={initial.until}
          className={fieldClass}
        />
      </label>
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
    <form
      onSubmit={submit}
      aria-label="Export evidence"
      className="space-y-4 rounded border border-slate-200 bg-white p-4"
    >
      <label className="flex items-start gap-2 text-sm font-medium">
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
          <span className="block text-xs font-normal text-slate-600">
            Every project, policy and rule. Clears the scope below, which is otherwise required.
          </span>
        </span>
      </label>
      <ScopeFields initial={initial} disabled={all} />
      <fieldset className="text-sm">
        <legend className="font-medium">Format</legend>
        <div className="mt-1 flex flex-wrap gap-4">
          {(
            [
              ["json", "JSON (one document)"],
              ["csv", "ZIP of CSV files"],
            ] as [ExportFormat, string][]
          ).map(([value, label]) => (
            <label key={value} className="flex items-center gap-2">
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
      <button type="submit" disabled={busy} className={buttonClass}>
        Export
      </button>
    </form>
  );
}

function Verification({ done }: { done: Extract<Download, { status: "done" }> | null }) {
  const file = done?.filename ?? "<file>";
  return (
    <section aria-labelledby="verify-title" className="space-y-2 text-sm text-slate-700">
      <h2 id="verify-title" className="text-lg font-semibold text-slate-900">
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
      <pre className="overflow-x-auto rounded bg-slate-900 p-3 font-mono text-xs text-slate-100">
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
      <div className="space-y-2 rounded border border-slate-200 bg-white p-4 text-sm">
        <p role="status">Exporting… The file is offered once every byte has arrived.</p>
        <p className="text-slate-600">{`${formatBytes(download.bytes)} received`}</p>
        <button type="button" onClick={cancel} className={secondaryClass}>
          Cancel export
        </button>
      </div>
    );
  }
  if (download.status === "done") {
    return (
      <p
        role="status"
        className="rounded border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-900"
      >
        {`Downloaded ${download.filename} (${formatBytes(download.bytes)}). Verify it before relying on it.`}
      </p>
    );
  }
  if (download.status === "cancelled") {
    return (
      <p role="status" className="text-sm text-slate-700">
        Export cancelled. Nothing was saved.
      </p>
    );
  }
  if (download.status === "failed") {
    return (
      <p role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-900">
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
    <div className="space-y-6">
      <ExportFormFields
        key={writeExportParams(readExportForm(params)).toString()}
        initial={readExportForm(params)}
        busy={download.status === "running"}
        onSubmit={(form) => void start(form)}
      />
      {errors.length > 0 && (
        <div
          role="alert"
          className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-900"
        >
          <p className="font-medium">The export was not started:</p>
          <ul className="mt-1 list-disc pl-5">
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

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Evidence
        </h1>
        <p className="mt-2 text-slate-600">
          Export findings, evaluations and the GitLab observations they judged, with collection
          errors and freshness. Unknown and failed collection state is exported, never dropped.
        </p>
      </div>
      <CollectionNote />
      {user && allowed ? (
        <Exporter csrfToken={user.csrf_token} />
      ) : (
        <p className="rounded border border-slate-200 bg-white p-4 text-sm text-slate-700">
          Exporting evidence needs the <code className={code}>{EXPORT_PERMISSION}</code> permission,
          which the admin and auditor roles have. Exports are bulk copies of inventory and evidence,
          so ask an administrator if you need one.
        </p>
      )}
    </section>
  );
}
