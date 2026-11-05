import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { frameworkIds } from "../api/controls";
import { saveDownload } from "../api/exports";
import { errorReasons } from "../api/send";
import { downloadPackage, getPackage, MAX_SAMPLE, type PackageRecord } from "../api/packages";
import { useAuth } from "../auth/context";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { frameworkLabels } from "../controls/format";
import { DEFAULT_SAMPLE, validatePackage, type PackageForm } from "./package";

type Run =
  | { status: "idle" }
  | { status: "running"; bytes: number }
  | {
      status: "done";
      filename: string;
      bytes: number;
      exportId: string | null;
      seed: string | null;
    }
  | { status: "cancelled" }
  | { status: "failed"; error: ApiError };

type RecordState =
  | { status: "loading" }
  | { status: "loaded"; record: PackageRecord }
  | { status: "failed"; error: ApiError };

const code = "rounded bg-slate-100 px-1 py-0.5 font-mono text-xs break-all";

function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

function formValues(form: FormData): PackageForm {
  return {
    from: text(form, "from"),
    to: text(form, "to"),
    framework: frameworkIds.find((id) => id === text(form, "framework")) ?? "",
    control: text(form, "control"),
    groups: text(form, "groups"),
    size: text(form, "size"),
    seed: text(form, "seed"),
  };
}

/** The package's recorded seed and manifest digest, read once the download has finished. */
function Recorded({ exportId, seed }: { exportId: string; seed: string | null }) {
  const [record, setRecord] = useState<RecordState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getPackage(exportId, controller.signal).then(
      (loaded) => {
        setRecord({ status: "loaded", record: loaded });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setRecord({
          status: "failed",
          error: error instanceof ApiError ? error : new ApiError(0, "It could not be read."),
        });
      },
    );
    return () => {
      controller.abort();
    };
  }, [exportId, attempt]);

  const recordedSeed =
    record.status === "loaded" && record.record.seed !== null ? String(record.record.seed) : null;
  const usedSeed = seed ?? recordedSeed;
  const digest = record.status === "loaded" ? record.record.manifest_sha256 : null;
  return (
    <dl className="mt-2 space-y-2">
      <div>
        <dt className="font-medium">Package</dt>
        <dd>
          <code className={code}>{exportId}</code>
        </dd>
      </div>
      <div>
        <dt className="font-medium">Seed used</dt>
        <dd>
          {usedSeed ? <code className={code}>{usedSeed}</code> : "Not reported"}
          <span className="block text-xs text-slate-700">
            The same seed over the same period draws the same sample, so the sample can be
            reproduced.
          </span>
        </dd>
      </div>
      <div>
        <dt className="font-medium">Manifest SHA-256</dt>
        <dd>
          {record.status === "loading" && <span role="status">Reading…</span>}
          {record.status === "failed" && <span role="alert">{record.error.message}</span>}
          {record.status === "loaded" &&
            (digest ? (
              <code className={code}>{digest}</code>
            ) : (
              <span>Not recorded yet; it is recorded once the download has finished.</span>
            ))}
          {record.status !== "loading" && !digest && (
            <button
              type="button"
              onClick={() => {
                setRecord({ status: "loading" });
                setAttempt((value) => value + 1);
              }}
              className={`ml-2 ${secondaryClass}`}
            >
              Check again
            </button>
          )}
        </dd>
      </div>
    </dl>
  );
}

function Outcome({ run, cancel }: { run: Run; cancel: () => void }) {
  if (run.status === "running") {
    return (
      <div className="space-y-2 text-sm">
        <p role="status">Building the package… It is offered once every byte has arrived.</p>
        <button type="button" onClick={cancel} className={secondaryClass}>
          Cancel package
        </button>
      </div>
    );
  }
  if (run.status === "done") {
    return (
      <div className="rounded border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-950">
        <p role="status">{`Downloaded ${run.filename}. Verify it before relying on it.`}</p>
        {run.exportId ? (
          <Recorded exportId={run.exportId} seed={run.seed} />
        ) : (
          <p className="mt-2">
            {`Seed used: ${run.seed ?? "not reported"}. The server did not name the package, so read its manifest digest from the audit log.`}
          </p>
        )}
      </div>
    );
  }
  if (run.status === "cancelled") {
    return (
      <p role="status" className="text-sm text-slate-700">
        Package cancelled. Nothing was saved.
      </p>
    );
  }
  if (run.status === "failed") {
    const reasons = errorReasons(run.error);
    return (
      <div
        role="alert"
        className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-900"
      >
        <p>{run.error.message}</p>
        {reasons.length > 0 && (
          <ul className="mt-1 list-disc pl-5" aria-label="Reasons">
            {reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  return null;
}

/** The auditor's package: a period, a scope, and a reproducible sample of the changes in it. */
export function PackageForm({ csrfToken }: { csrfToken: string }) {
  const { refresh } = useAuth();
  const [errors, setErrors] = useState<string[]>([]);
  const [run, setRun] = useState<Run>({ status: "idle" });
  const controller = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const checked = validatePackage(formValues(new FormData(event.currentTarget)));
    if ("errors" in checked) {
      setErrors(checked.errors);
      return;
    }
    setErrors([]);
    const current = new AbortController();
    controller.current = current;
    setRun({ status: "running", bytes: 0 });
    try {
      const result = await downloadPackage(checked.request, csrfToken, current.signal, (bytes) => {
        setRun({ status: "running", bytes });
      });
      saveDownload(result);
      setRun({
        status: "done",
        filename: result.filename,
        bytes: result.bytes,
        exportId: result.exportId,
        seed: result.seed,
      });
    } catch (error) {
      if (current.signal.aborted) {
        setRun({ status: "cancelled" });
        return;
      }
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The package could not be read.");
      setRun({ status: "failed", error: apiError });
      if (apiError.status === 401) void refresh();
    } finally {
      if (controller.current === current) controller.current = null;
    }
  }

  return (
    <section aria-labelledby="package-title" className="space-y-4">
      <div>
        <h2 id="package-title" className="text-lg font-semibold">
          Audit package
        </h2>
        <p className="mt-1 max-w-3xl text-sm text-slate-700">
          Everything an auditor needs for a period: findings and results as they stood at its end,
          every change in it, a sample of those changes with their full evidence, and the control
          mapping and monitoring scope in force.
        </p>
      </div>
      <form
        onSubmit={(event) => void submit(event)}
        aria-label="Create an audit package"
        className="space-y-4 rounded border border-slate-200 bg-white p-4"
      >
        <fieldset className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <legend className="text-sm font-medium">Period and scope</legend>
          <label className="text-sm font-medium">
            From (UTC)
            <input name="from" type="datetime-local" className={fieldClass} />
          </label>
          <label className="text-sm font-medium">
            Up to (UTC)
            <input name="to" type="datetime-local" className={fieldClass} />
          </label>
          <div>
            <label className="text-sm font-medium">
              Group paths
              <input
                name="groups"
                placeholder="acme/payments, acme/platform"
                aria-describedby="package-groups-help"
                className={fieldClass}
              />
            </label>
            <p id="package-groups-help" className="mt-1 text-xs text-slate-600">
              Optional; separated by commas. Every monitored project when empty.
            </p>
          </div>
          <label className="text-sm font-medium">
            Framework
            <select name="framework" defaultValue="" className={fieldClass}>
              <option value="">Any</option>
              {frameworkIds.map((framework) => (
                <option key={framework} value={framework}>
                  {frameworkLabels[framework]}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm font-medium">
            Control ID
            <input name="control" maxLength={20} placeholder="CC8.1" className={fieldClass} />
          </label>
        </fieldset>
        <fieldset className="grid gap-3 sm:grid-cols-2">
          <legend className="text-sm font-medium">Sample of changes</legend>
          <label className="text-sm font-medium">
            Sample size
            <input
              name="size"
              type="number"
              min={1}
              max={MAX_SAMPLE}
              step={1}
              defaultValue={DEFAULT_SAMPLE}
              className={fieldClass}
            />
          </label>
          <div>
            <label className="text-sm font-medium">
              Seed
              <input
                name="seed"
                inputMode="numeric"
                aria-describedby="package-seed-help"
                className={fieldClass}
              />
            </label>
            <p id="package-seed-help" className="mt-1 text-xs text-slate-600">
              Optional. Leave empty to have one chosen; enter an earlier seed to draw the same
              sample again.
            </p>
          </div>
        </fieldset>
        <button type="submit" disabled={run.status === "running"} className={buttonClass}>
          Create package
        </button>
      </form>
      {errors.length > 0 && (
        <div
          role="alert"
          className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-900"
        >
          <p className="font-medium">The package was not started:</p>
          <ul className="mt-1 list-disc pl-5">
            {errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      )}
      <Outcome
        run={run}
        cancel={() => {
          controller.current?.abort();
        }}
      />
    </section>
  );
}
