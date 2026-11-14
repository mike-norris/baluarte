/**
 * Pass, fail and unknown counts from the latest successful evaluation run
 * (``GET /api/v1/evaluations/summary``). Unknown and unsupported are never counted as passing.
 */

import { Link } from "react-router";

import type { EvaluationSummary, ResultCounts } from "../api/evaluations";
import { formatTime } from "../time";

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

/** "3 failing · 95 passing · 2 unknown", leaving out zero counts other than passing. */
export function countParts(counts: ResultCounts): { key: string; text: string }[] {
  const unknown = counts.unknown + counts.unsupported;
  const parts = [
    { key: "fail", count: counts.fail, text: `${String(counts.fail)} failing` },
    { key: "pass", count: counts.pass, text: `${String(counts.pass)} passing` },
    { key: "unknown", count: unknown, text: `${String(unknown)} unknown` },
    {
      key: "excepted",
      count: counts.excepted,
      text: `${String(counts.excepted)} excepted`,
    },
  ];
  return parts.filter((part) => part.key === "pass" || part.count > 0);
}

export function total(counts: ResultCounts): number {
  return counts.pass + counts.fail + counts.unknown + counts.unsupported + counts.excepted;
}

const tone: Record<string, string> = {
  fail: "text-danger-emphasis fw-medium",
  pass: "text-success-emphasis",
  unknown: "text-secondary",
  excepted: "text-indigo",
};

/**
 * Counts on one line. ``failingHref`` links the failing count to the matching open findings.
 * ``noun`` is what is counted: projects for a baseline, results for a rule.
 */
export function ResultLine({
  counts,
  failingHref,
  noun,
}: {
  counts: ResultCounts;
  failingHref?: string;
  noun?: [string, string];
}) {
  const parts = countParts(counts);
  return (
    <span className="d-inline-flex flex-wrap gap-x-2">
      {noun && <span className="text-secondary">{`${plural(total(counts), ...noun)}:`}</span>}
      {parts.map((part, index) => (
        <span key={part.key} className={tone[part.key]}>
          {index > 0 && (
            <span aria-hidden="true" className="me-2 text-secondary">
              ·
            </span>
          )}
          {part.key === "fail" && failingHref ? (
            <Link to={failingHref}>{part.text}</Link>
          ) : (
            part.text
          )}
        </span>
      ))}
    </span>
  );
}

/** When the counts are from, and whether they may be out of date. */
export function ResultsAsOf({ summary }: { summary: EvaluationSummary }) {
  const run = summary.evaluation_run;
  if (!run) {
    return (
      <p className="text-secondary">
        No evaluation has finished yet, so there are no results to count.
      </p>
    );
  }
  const stale = summary.freshness.status === "stale" || summary.freshness.status === "failed";
  return (
    <p className={stale ? "text-warning-emphasis" : "text-secondary"}>
      {`Results from the evaluation finished ${formatTime(run.finished_at ?? run.started_at)}.`}
      {stale && " They may be out of date: an old pass is not current compliance."}
      {summary.freshness.running && " A new evaluation is running."}
    </p>
  );
}
