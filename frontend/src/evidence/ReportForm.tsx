import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { frameworkIds } from "../api/controls";
import { saveDownload } from "../api/exports";
import { downloadReport } from "../api/reports";
import { errorReasons } from "../api/send";
import { useAuth } from "../auth/context";
import { frameworkLabels } from "../controls/format";
import { validateReport, type ReportForm as ReportFields } from "./report";

type Run =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; filename: string; reportId: string | null }
  | { status: "failed"; error: ApiError };

function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

function formValues(form: FormData): ReportFields {
  return {
    framework: frameworkIds.find((id) => id === text(form, "framework")) ?? "",
    from: text(form, "from"),
    to: text(form, "to"),
    groups: text(form, "groups"),
  };
}

function Outcome({ run }: { run: Run }) {
  if (run.status === "running") {
    return <p role="status">Building the report… It is offered once it has fully arrived.</p>;
  }
  if (run.status === "done") {
    return (
      <div role="status" className="alert alert-success text-body">
        <p>{`Downloaded ${run.filename}.`}</p>
        <p className="mt-1 mb-0">
          {run.reportId ? (
            <>
              Report <code className="text-break">{run.reportId}</code>.{" "}
            </>
          ) : null}
          It summarizes the evidence; the audit package for the same period is the complete,
          verifiable record.
        </p>
      </div>
    );
  }
  if (run.status === "failed") {
    const reasons = errorReasons(run.error);
    return (
      <div role="alert" className="alert alert-danger text-body">
        <p>{run.error.message}</p>
        {reasons.length > 0 && (
          <ul className="mt-1 ps-3" aria-label="Reasons">
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

/** The auditor report: one framework's controls over a period, as a PDF. */
export function ReportForm({ csrfToken }: { csrfToken: string }) {
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
    const checked = validateReport(formValues(new FormData(event.currentTarget)));
    if ("errors" in checked) {
      setErrors(checked.errors);
      return;
    }
    setErrors([]);
    const current = new AbortController();
    controller.current = current;
    setRun({ status: "running" });
    try {
      const result = await downloadReport(checked.request, csrfToken, current.signal, () => {
        // The report is small; progress is not shown.
      });
      saveDownload(result);
      setRun({ status: "done", filename: result.filename, reportId: result.exportId });
    } catch (error) {
      if (current.signal.aborted) {
        setRun({ status: "idle" });
        return;
      }
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The report could not be read.");
      setRun({ status: "failed", error: apiError });
      if (apiError.status === 401) void refresh();
    } finally {
      if (controller.current === current) controller.current = null;
    }
  }

  return (
    <section className="stack-4 mb-4" aria-labelledby="report-title">
      <div>
        <h2 id="report-title" className="card-title">
          Auditor report
        </h2>
        <p className="mt-1 mw-measure text-secondary">
          A readable PDF for one framework and period: each control’s status as of the period’s end,
          what supports it, failing projects, exceptions, attestations and exclusions, and how to
          check it against the audit package. Supporting evidence, not a certification.
        </p>
      </div>
      <form
        onSubmit={(event) => void submit(event)}
        aria-label="Download the auditor report"
        className="card card-body stack-4"
      >
        <fieldset className="d-grid gap-3 cols-sm-2 cols-lg-3">
          <legend className="fw-medium">Framework, period and scope</legend>
          <label className="fw-medium">
            Framework
            <select name="framework" defaultValue="" className="form-select mt-1">
              <option value="">Choose…</option>
              {frameworkIds.map((framework) => (
                <option key={framework} value={framework}>
                  {frameworkLabels[framework]}
                </option>
              ))}
            </select>
          </label>
          <label className="fw-medium">
            From (UTC)
            <input name="from" type="datetime-local" className="form-control mt-1" />
          </label>
          <label className="fw-medium">
            Up to (UTC)
            <input name="to" type="datetime-local" className="form-control mt-1" />
          </label>
          <div>
            <label className="fw-medium">
              Group paths
              <input
                name="groups"
                placeholder="acme/payments, acme/platform"
                aria-describedby="report-groups-help"
                className="form-control mt-1"
              />
            </label>
            <p id="report-groups-help" className="mt-1 small text-secondary">
              Optional; separated by commas. Every monitored project when empty.
            </p>
          </div>
        </fieldset>
        <div>
          <button type="submit" disabled={run.status === "running"} className="btn btn-primary">
            Download PDF
          </button>
        </div>
      </form>
      {errors.length > 0 && (
        <div role="alert" className="alert alert-danger text-body">
          <p className="fw-medium">The report was not requested:</p>
          <ul className="mt-1 ps-3">
            {errors.map((error) => (
              <li key={error}>{error}</li>
            ))}
          </ul>
        </div>
      )}
      <Outcome run={run} />
    </section>
  );
}
