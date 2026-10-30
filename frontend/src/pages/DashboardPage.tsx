import type { ReactNode } from "react";
import { Link } from "react-router";

import {
  FINDINGS_MAX_PAGES,
  FINDINGS_PAGE,
  collectFindings,
  findingsHref,
  isSeverity,
  latestSuccessfulRun,
  outcomes,
  severities,
  type EvaluationFreshness,
  type EvaluationRun,
  type Finding,
} from "../api/evaluations";
import { useLoad, type Loaded } from "../api/useLoad";
import { outcomeLabels, outcomeMeaning, severityLabels } from "../findings/format";
import { useFreshness } from "../freshness/context";
import { freshnessTone, needsAttention, statusLabel } from "../freshness/status";
import { formatTime } from "../time";

const linkClass = "text-sky-800 underline";
const cell = "px-3 py-2";
const tableClass = "mt-3 w-full border-collapse text-left text-sm";
const headClass = "border-b border-slate-300 bg-slate-100";

function evaluationHeadline(report: EvaluationFreshness): string {
  const hours = Math.round(report.stale_after_minutes / 60);
  switch (report.status) {
    case "failed":
      return "The latest policy evaluation failed. Results below may be out of date.";
    case "stale":
      return `Compliance results are stale: the last successful evaluation is more than ${String(hours)} hours old.`;
    case "never":
      return "No policy evaluation has completed yet, so there are no compliance results.";
    case "not_configured":
      return "GitLab is not configured, so nothing can be evaluated.";
    case "fresh":
      return "Compliance results are up to date.";
  }
}

function EvaluationStatus() {
  const freshness = useFreshness();
  const signal = freshness?.signals.find((entry) => entry.source === "evaluation");
  if (!freshness || !signal) return null;
  const report = freshness.evaluation;
  const attention = needsAttention(signal.status);
  return (
    <section
      aria-labelledby="evaluation-status"
      className={`mt-6 rounded border p-4 ${freshnessTone[signal.status]}`}
    >
      <h2 id="evaluation-status" className="text-lg font-semibold">
        {`Evaluation status: ${statusLabel("evaluation", signal.status)}`}
      </h2>
      {freshness.evaluationError ? (
        <p role="alert" className="mt-1">
          {`Evaluation status could not be checked: ${freshness.evaluationError.message}`}
        </p>
      ) : !report ? (
        <p role="status" className="mt-1">
          Checking evaluation status…
        </p>
      ) : (
        <>
          <p role={attention ? "alert" : undefined} className="mt-1">
            {evaluationHeadline(report)}
          </p>
          <ul className="mt-2 space-y-1 text-sm">
            <li>{`Last successful evaluation: ${formatTime(report.last_success_at)}`}</li>
            {report.running && <li>An evaluation is running now.</li>}
            {report.queued && !report.running && <li>An evaluation is queued.</li>}
            {report.behind_collection && (
              <li>
                GitLab data was collected after the last successful evaluation started, so these
                results may not reflect it yet
                {report.running || report.queued ? " (an evaluation is on its way)." : "."}
              </li>
            )}
            {report.last_run?.status === "failed" && report.last_run.error && (
              <li>{`Last run failed: ${report.last_run.error}`}</li>
            )}
          </ul>
        </>
      )}
    </section>
  );
}

function LoadState<T>({
  loaded,
  noun,
  children,
}: {
  loaded: Loaded<T>;
  noun: string;
  children: (data: T) => ReactNode;
}) {
  if (loaded.error) {
    const retry = loaded.error.status !== 401 && loaded.error.status !== 403;
    return (
      <div role="alert" className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-sm">
        <p>{`Could not load ${noun}: ${loaded.error.message}`}</p>
        {retry && (
          <button
            type="button"
            onClick={loaded.retry}
            className="mt-2 rounded border border-slate-400 bg-white px-2 py-1"
          >
            Try again
          </button>
        )}
      </div>
    );
  }
  if (loaded.loading || loaded.data === undefined) {
    return (
      <p role="status" className="mt-3 text-sm text-slate-600">
        {`Loading ${noun}…`}
      </p>
    );
  }
  return <>{children(loaded.data)}</>;
}

/** Floored, so a share just below 100% never reads as 100%. */
function percent(part: number, whole: number): string {
  return `${String(Math.floor((part * 1000) / whole) / 10)}%`;
}

function RuleResults({ run, newest }: { run: EvaluationRun | null; newest: EvaluationRun | null }) {
  if (!run) {
    return (
      <p className="mt-3 text-sm">
        No evaluation has completed successfully yet, so there are no rule results.
        {newest?.status === "failed" && newest.error
          ? ` The latest run failed: ${newest.error}`
          : ""}
      </p>
    );
  }
  const counts = run.counts.outcomes ?? {};
  const total = outcomes.reduce((sum, outcome) => sum + (counts[outcome] ?? 0), 0);
  const passed = counts.pass ?? 0;
  return (
    <>
      <p className="mt-3 text-sm">
        {`From evaluation run ${String(run.id)}, finished ${formatTime(run.finished_at)}, covering ${String(run.projects)} projects.`}
      </p>
      {newest && newest.id !== run.id && (
        <p className="mt-1 text-sm font-medium">
          {`A newer run (${String(newest.id)}) has not succeeded, so these totals come from an earlier run.`}
        </p>
      )}
      {total === 0 ? (
        <p className="mt-3 text-sm">
          The run produced no rule results: no policy applies to any project.
        </p>
      ) : (
        <p className="mt-3 text-sm">
          {`${String(passed)} of ${String(total)} rule results passed (${percent(passed, total)}). `}
          The rest failed, were waived by an exception, could not be decided or could not be
          checked; none of them count as passing.
        </p>
      )}
      <table className={tableClass}>
        <caption className="sr-only">Rule results by outcome</caption>
        <thead className={headClass}>
          <tr>
            <th scope="col" className={cell}>
              Outcome
            </th>
            <th scope="col" className={cell}>
              Results
            </th>
            <th scope="col" className={cell}>
              Meaning
            </th>
          </tr>
        </thead>
        <tbody>
          {outcomes.map((outcome) => (
            <tr key={outcome} className="border-b border-slate-200">
              <th scope="row" className={`${cell} font-medium`}>
                {outcomeLabels[outcome]}
              </th>
              <td className={cell}>{counts[outcome] ?? 0}</td>
              <td className={cell}>{outcomeMeaning[outcome]}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function OpenFindings({ items, truncated }: { items: Finding[]; truncated: boolean }) {
  const prefix = truncated ? "At least " : "";
  if (items.length === 0) {
    return (
      <p className="mt-3 text-sm">
        No open findings. This does not mean every project complies: unknown and unsupported results
        never open a finding.
      </p>
    );
  }
  const bySeverity = new Map<string, number>();
  const byRule = new Map<string, { policy: string; rule: string; count: number }>();
  let undecided = 0;
  for (const finding of items) {
    bySeverity.set(finding.severity, (bySeverity.get(finding.severity) ?? 0) + 1);
    const key = `${finding.policy.name}\u0000${finding.rule_id}`;
    const entry = byRule.get(key) ?? {
      policy: finding.policy.name,
      rule: finding.rule_id,
      count: 0,
    };
    entry.count += 1;
    byRule.set(key, entry);
    if (finding.latest_outcome === "unknown" || finding.latest_outcome === "unsupported") {
      undecided += 1;
    }
  }
  const rules = [...byRule.values()].sort(
    (a, b) => b.count - a.count || a.policy.localeCompare(b.policy) || a.rule.localeCompare(b.rule),
  );
  // Severities outside the known four are still shown, never dropped.
  const other = [...bySeverity.keys()].filter((severity) => !isSeverity(severity));
  return (
    <>
      <p className="mt-3 text-sm">
        {`${prefix}${String(items.length)} open findings.`}
        {truncated &&
          ` Only the first ${String(FINDINGS_MAX_PAGES * FINDINGS_PAGE)} were counted, so the totals below are lower bounds.`}
      </p>
      {undecided > 0 && (
        <p className="mt-1 text-sm">
          {`${prefix}${String(undecided)} of them could not be re-checked: their latest result is unknown or unsupported, so they stay open. `}
          <Link to={findingsHref({ state: "open", outcome: "unknown" })} className={linkClass}>
            Open findings with an unknown latest result
          </Link>
        </p>
      )}
      <table className={tableClass}>
        <caption className="sr-only">Open findings by severity</caption>
        <thead className={headClass}>
          <tr>
            <th scope="col" className={cell}>
              Severity
            </th>
            <th scope="col" className={cell}>
              Open findings
            </th>
          </tr>
        </thead>
        <tbody>
          {[...severities, ...other].map((severity) => {
            const count = bySeverity.get(severity) ?? 0;
            const label = severityLabels[severity] ?? severity;
            return (
              <tr key={severity} className="border-b border-slate-200">
                <th scope="row" className={`${cell} font-medium`}>
                  {label}
                </th>
                <td className={cell}>
                  {count === 0 && !truncated ? (
                    "0"
                  ) : (
                    <Link
                      to={findingsHref(
                        isSeverity(severity) ? { state: "open", severity } : { state: "open" },
                      )}
                      className={linkClass}
                      aria-label={`${prefix}${String(count)} open ${label.toLowerCase()} findings`}
                    >
                      {`${prefix}${String(count)}`}
                    </Link>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <table className={tableClass}>
        <caption className="sr-only">Open findings by rule</caption>
        <thead className={headClass}>
          <tr>
            <th scope="col" className={cell}>
              Policy
            </th>
            <th scope="col" className={cell}>
              Rule
            </th>
            <th scope="col" className={cell}>
              Open findings
            </th>
          </tr>
        </thead>
        <tbody>
          {rules.map(({ policy, rule, count }) => (
            <tr key={`${policy}/${rule}`} className="border-b border-slate-200">
              <td className={cell}>{policy}</td>
              <th scope="row" className={`${cell} font-medium`}>
                {rule}
              </th>
              <td className={cell}>
                <Link
                  to={findingsHref({ state: "open", policy, rule })}
                  className={linkClass}
                  aria-label={`${prefix}${String(count)} open findings for ${policy} ${rule}`}
                >
                  {`${prefix}${String(count)}`}
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function DashboardPage() {
  const runs = useLoad("dashboard-runs", (signal) => latestSuccessfulRun(signal));
  const findings = useLoad("dashboard-findings", (signal) =>
    collectFindings({ state: "open" }, signal),
  );
  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        Dashboard
      </h1>
      <p className="mt-2 text-slate-600">Compliance posture across groups and projects.</p>
      <EvaluationStatus />
      <section aria-labelledby="rule-results" className="mt-8">
        <h2 id="rule-results" className="text-lg font-semibold">
          Rule results
        </h2>
        <LoadState loaded={runs} noun="rule results">
          {(data) => <RuleResults run={data.run} newest={data.newest} />}
        </LoadState>
      </section>
      <section aria-labelledby="open-findings" className="mt-8">
        <h2 id="open-findings" className="text-lg font-semibold">
          Open findings
        </h2>
        <LoadState loaded={findings} noun="open findings">
          {(data) => <OpenFindings items={data.items} truncated={data.truncated} />}
        </LoadState>
        <p className="mt-3 text-sm">
          <Link to="/findings" className={linkClass}>
            All findings
          </Link>
        </p>
      </section>
    </section>
  );
}
