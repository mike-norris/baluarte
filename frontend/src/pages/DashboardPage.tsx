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
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { outcomeLabels, outcomeMeaning, severityLabels } from "../findings/format";
import { useFreshness } from "../freshness/context";
import { PopulationNote } from "../freshness/PopulationNote";
import { freshnessColor, needsAttention, statusLabel } from "../freshness/status";
import { APPLY_VIEW } from "../policies/format";
import { formatTime } from "../time";
import { KpiCard } from "../ui/KpiCard";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

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
      className={`card card-body mb-3 border-${freshnessColor[signal.status]}`}
    >
      <h2 id="evaluation-status" className="card-title d-flex align-items-center gap-2 mb-1">
        <span className={`status-dot status-${freshnessColor[signal.status]}`} aria-hidden="true" />
        {`Evaluation status: ${statusLabel("evaluation", signal.status)}`}
      </h2>
      {freshness.evaluationError ? (
        <p role="alert" className="mb-0">
          {`Evaluation status could not be checked: ${freshness.evaluationError.message}`}
        </p>
      ) : !report ? (
        <p role="status" className="mb-0 text-secondary">
          Checking evaluation status…
        </p>
      ) : (
        <>
          <p role={attention ? "alert" : undefined} className="mb-2">
            {evaluationHeadline(report)}
          </p>
          <ul className="mb-0 text-secondary">
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
      <div role="alert" className="alert alert-danger m-3">
        <p className="mb-0 text-body">{`Could not load ${noun}: ${loaded.error.message}`}</p>
        {retry && (
          <button type="button" onClick={loaded.retry} className="btn btn-sm mt-2">
            Try again
          </button>
        )}
      </div>
    );
  }
  if (loaded.loading || loaded.data === undefined) {
    return (
      <div className="card-body">
        <Loading label={`Loading ${noun}…`} />
      </div>
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
      <p className="card-body mb-0">
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
      <p className="card-body mb-0">
        {`From evaluation run ${String(run.id)}, finished ${formatTime(run.finished_at)}, covering ${String(run.projects)} projects.`}
      </p>
      {newest && newest.id !== run.id && (
        <p className="card-body pt-0 mb-0 fw-medium">
          {`A newer run (${String(newest.id)}) has not succeeded, so these totals come from an earlier run.`}
        </p>
      )}
      {total === 0 ? (
        <p className="card-body mb-0">
          {"The run produced no rule results: no baseline applies to any project. "}
          <Link to={APPLY_VIEW}>See where baselines apply</Link>
          {", or open a project or group from the "}
          <Link to="/inventory">inventory</Link>
          {" to see what reaches it."}
        </p>
      ) : (
        <p className="card-body mb-0">
          {`${String(passed)} of ${String(total)} rule results passed (${percent(passed, total)}). `}
          The rest failed, were waived by an exception, could not be decided or could not be
          checked; none of them count as passing.
        </p>
      )}
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">Rule results by outcome</caption>
        <thead>
          <tr>
            <th scope="col">Outcome</th>
            <th scope="col">Results</th>
            <th scope="col">Meaning</th>
          </tr>
        </thead>
        <tbody>
          {outcomes.map((outcome) => (
            <tr key={outcome}>
              <th scope="row" className="fw-medium">
                {outcomeLabels[outcome]}
              </th>
              <td>{counts[outcome] ?? 0}</td>
              <td>{outcomeMeaning[outcome]}</td>
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
      <p className="card-body mb-0">
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
      <p className="card-body mb-0">
        {`${prefix}${String(items.length)} open findings.`}
        {truncated &&
          ` Only the first ${String(FINDINGS_MAX_PAGES * FINDINGS_PAGE)} were counted, so the totals below are lower bounds.`}
      </p>
      {undecided > 0 && (
        <p className="card-body pt-0 mb-0">
          {`${prefix}${String(undecided)} of them could not be re-checked: their latest result is unknown or unsupported, so they stay open. `}
          <Link to={findingsHref({ state: "open", outcome: "unknown" })}>
            Open findings with an unknown latest result
          </Link>
        </p>
      )}
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">Open findings by severity</caption>
        <thead>
          <tr>
            <th scope="col">Severity</th>
            <th scope="col">Open findings</th>
          </tr>
        </thead>
        <tbody>
          {[...severities, ...other].map((severity) => {
            const count = bySeverity.get(severity) ?? 0;
            const label = severityLabels[severity] ?? severity;
            return (
              <tr key={severity}>
                <th scope="row" className="fw-medium">
                  {label}
                </th>
                <td>
                  {count === 0 && !truncated ? (
                    "0"
                  ) : (
                    <Link
                      to={findingsHref(
                        isSeverity(severity) ? { state: "open", severity } : { state: "open" },
                      )}

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
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">Open findings by rule</caption>
        <thead>
          <tr>
            <th scope="col">Policy</th>
            <th scope="col">Rule</th>
            <th scope="col">Open findings</th>
          </tr>
        </thead>
        <tbody>
          {rules.map(({ policy, rule, count }) => (
            <tr key={`${policy}/${rule}`}>
              <td>{policy}</td>
              <th scope="row" className="fw-medium">
                {rule}
              </th>
              <td>
                <Link
                  to={findingsHref({ state: "open", policy, rule })}

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

/** Headline figures; each says what it counts, and unknown never reads as passing. */
function Figures({
  run,
  findings,
}: {
  run: EvaluationRun | null | undefined;
  findings: { items: Finding[]; truncated: boolean } | undefined;
}) {
  const counts = run?.counts.outcomes ?? {};
  const total = outcomes.reduce((sum, outcome) => sum + (counts[outcome] ?? 0), 0);
  const passed = counts.pass ?? 0;
  const undecided = (counts.unknown ?? 0) + (counts.unsupported ?? 0);
  const prefix = findings?.truncated ? "≥" : "";
  const urgent = findings?.items.filter(
    (finding) => finding.severity === "critical" || finding.severity === "high",
  ).length;
  return (
    <div className="row row-cards mb-3">
      <div className="col-6 col-lg-3">
        <KpiCard
          label="Rule results passed"
          value={run && total > 0 ? percent(passed, total) : "—"}
          footnote={
            run && total > 0 ? `${String(passed)} of ${String(total)} results` : "No results yet"
          }
        />
      </div>
      <div className="col-6 col-lg-3">
        <KpiCard
          label="Open findings"
          value={findings ? `${prefix}${String(findings.items.length)}` : "—"}
          footnote="All severities"
          href="/findings"
        />
      </div>
      <div className="col-6 col-lg-3">
        <KpiCard
          label="Critical and high"
          value={urgent === undefined ? "—" : `${prefix}${String(urgent)}`}
          footnote="Open findings"
          href={findingsHref({ state: "open" })}
        />
      </div>
      <div className="col-6 col-lg-3">
        <KpiCard
          label="Could not be decided"
          value={run ? String(undecided) : "—"}
          footnote="Unknown or unsupported; never passing"
        />
      </div>
    </div>
  );
}

export function DashboardPage() {
  const asOf = useAsOf();
  const runs = useLoad(`dashboard-runs@${asOf ?? "now"}`, (signal) =>
    latestSuccessfulRun(signal, undefined, asOf),
  );
  const findings = useLoad(`dashboard-findings@${asOf ?? "now"}`, (signal) =>
    collectFindings({ state: "open" }, signal, undefined, asOf),
  );
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Overview"
        title="Dashboard"
        subtitle="Compliance posture across groups and projects."
        actions={
          <Link to="/findings" className="btn">
            All findings
          </Link>
        }
      />
      <PopulationNote />
      {asOf ? (
        <div className="mb-3">
          <AsOfResult answer={runs.data?.answer} />
        </div>
      ) : (
        <EvaluationStatus />
      )}
      <Figures run={runs.data?.run} findings={findings.data} />
      <div className="row row-cards">
        <div className="col-xl-7">
          <section aria-labelledby="rule-results" className="card">
            <div className="card-header">
              <h2 id="rule-results" className="card-title">
                Rule results
              </h2>
            </div>
            <LoadState loaded={runs} noun="rule results">
              {(data) => <RuleResults run={data.run} newest={data.newest} />}
            </LoadState>
          </section>
        </div>
        <div className="col-xl-5">
          <section aria-labelledby="open-findings" className="card">
            <div className="card-header">
              <h2 id="open-findings" className="card-title">
                Open findings
              </h2>
            </div>
            <LoadState loaded={findings} noun="open findings">
              {(data) => <OpenFindings items={data.items} truncated={data.truncated} />}
            </LoadState>
          </section>
        </div>
      </div>
    </section>
  );
}
