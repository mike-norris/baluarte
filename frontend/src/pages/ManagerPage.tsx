import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import type { FrameworkId } from "../api/controls";
import { groupHref } from "../api/gitlab";
import {
  getOverview,
  getTrends,
  MAX_BUCKETS,
  percent,
  rowName,
  type Bucket,
  type ChangeWindow,
  type OverviewRow,
  type Trends,
} from "../api/manager";
import { formatDuration, parseDate } from "../api/time";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass } from "../components/styles";
import { frameworkLabels } from "../controls/format";
import {
  currentOnlyText,
  focusText,
  MANAGER,
  MANAGER_TIME,
  parentPath,
  readFocus,
  writeFocus,
} from "../manager/format";
import { LineChart } from "../manager/LineChart";
import { FocusPicker, ManagerTabs } from "../manager/parts";

function Header({ title, intro }: { title: string; intro: string }) {
  return (
    <div className="space-y-3">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          {title}
        </h1>
        <p className="mt-2 text-slate-600">{intro}</p>
      </div>
      <ManagerTabs />
    </div>
  );
}

function changeText(window: ChangeWindow): string {
  if (window.total === 0) return "No changes";
  const parts = [`${percent(window.rate)} reviewed (${String(window.reviewed)} of ${String(window.total)})`];
  if (window.direct_pushes > 0) parts.push(`${String(window.direct_pushes)} direct pushes`);
  if (window.unknown > 0) parts.push(`${String(window.unknown)} unknown`);
  return parts.join(", ");
}

function Figure({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="contents">
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function RowCard({ row, focusTag }: { row: OverviewRow; focusTag: string | undefined }) {
  const [params] = useSearchParams();
  const name = rowName(row.key);
  const tag = row.key.type === "tag" ? `${row.key.key}:${row.key.value}` : focusTag;
  const group = row.key.type === "group" ? row.key.path : undefined;
  const findings = new URLSearchParams({ state: "open" });
  if (tag) findings.set("tag", tag);
  const frameworks = Object.entries(row.controls_not_met) as [FrameworkId, number][];
  const topLevel = group?.split("/")[0];
  const time = writeFocus(group ? { group } : tag ? { tag } : {});
  return (
    <article aria-labelledby={`row-${name}`} className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={`row-${name}`} className="font-semibold">
          {row.key.type === "group" ? (
            <Link to={`${MANAGER}?${writeFocus({ group: row.key.path }, params).toString()}`} className={linkClass}>
              {name}
            </Link>
          ) : (
            name
          )}
        </h2>
        {row.key.type === "group" && (
          <Link to={groupHref(row.key.gitlab_id)} className={`text-sm ${linkClass}`}>
            In the inventory
          </Link>
        )}
      </div>
      <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
        <Figure term="Projects">
          {`${String(row.projects.monitored)} monitored`}
          {row.projects.excluded > 0 && `, ${String(row.projects.excluded)} excluded`}
        </Figure>
        <Figure term="Open findings">
          <Link to={`/findings?${findings.toString()}`} className={linkClass}>
            {`${String(row.findings_open.total)} open`}
          </Link>
          {row.findings_open.total > 0 &&
            `: ${String(row.findings_open.critical)} critical, ${String(row.findings_open.high)} high, ${String(row.findings_open.medium)} medium, ${String(row.findings_open.low)} low`}
        </Figure>
        <Figure term="Controls not met">
          {frameworks.length === 0 ? (
            "None"
          ) : (
            <Link to="/controls" className={linkClass}>
              {frameworks
                .map(([framework, count]) => `${frameworkLabels[framework] ?? framework}: ${String(count)}`)
                .join("; ")}
            </Link>
          )}
        </Figure>
        <Figure term="Changes, last 30 days">
          <Link to="/changes" className={linkClass}>
            {changeText(row.changes.days_30)}
          </Link>
        </Figure>
        <Figure term="Changes, last 90 days">{changeText(row.changes.days_90)}</Figure>
        <Figure term="Label drift">
          {row.label_drift === null ? (
            "No label taxonomy in force"
          ) : (
            <Link to={topLevel ? `/labels?group=${encodeURIComponent(topLevel)}` : "/labels"} className={linkClass}>
              {`${String(row.label_drift)} items`}
            </Link>
          )}
        </Figure>
        <Figure term="Time, last 30 days">
          {row.time_30d === null ? (
            "Not collected yet"
          ) : (
            <Link to={`${MANAGER_TIME}?${time.toString()}`} className={linkClass}>
              {`${formatDuration(row.time_30d.spent_seconds)} spent, ${row.time_30d.estimate_seconds === null ? "no estimate" : `${formatDuration(row.time_30d.estimate_seconds)} estimated`}`}
            </Link>
          )}
        </Figure>
      </dl>
    </article>
  );
}

/** Results rolled up by child group or tag value, each figure linking to its list. */
export function ManagerPage() {
  const [params] = useSearchParams();
  const focus = readFocus(params);
  const asOf = useAsOf();
  const key = `overview?${writeFocus(focus).toString()}@${asOf ?? "now"}`;
  const overview = useLoad(key, (signal) => getOverview(focus, signal, asOf));
  const parent = focus.group ? parentPath(focus.group) : null;
  const note = overview.data && asOf ? currentOnlyText(overview.data.current_only) : null;

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <Header
        title="Manager"
        intro="Posture, change review, label consistency and time across the groups and teams you follow. Nothing here changes GitLab."
      />
      <FocusPicker />
      {focus.group && (
        <p className="text-sm">
          <Link
            to={`${MANAGER}?${writeFocus(parent ? { group: parent } : {}, params).toString()}`}
            className={linkClass}
          >
            {parent ? `Up to ${parent}` : "Back to all top-level groups"}
          </Link>
        </p>
      )}
      <AsOfResult answer={overview.data} />
      {note && <p className="text-sm text-slate-700">{note}</p>}
      {overview.error ? (
        <ErrorMessage error={overview.error} retry={overview.retry} />
      ) : overview.data ? (
        overview.data.rows.length === 0 ? (
          <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
            {`Nothing to show for ${focusText(focus)}.`}
          </p>
        ) : (
          <div className="grid gap-4 lg:grid-cols-2">
            {overview.data.rows.map((row) => (
              <RowCard key={rowName(row.key)} row={row} focusTag={focus.tag} />
            ))}
          </div>
        )
      ) : (
        <p role="status" className="text-slate-600">
          Loading the overview…
        </p>
      )}
    </section>
  );
}

const dateFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  day: "numeric",
  month: "short",
  year: "numeric",
});

function bucketLabel(end: string): string {
  return dateFormat.format(new Date(end));
}

const count = (value: number) => String(Math.round(value));
const share = (value: number) => `${String(Math.round(value * 100))}%`;
const hours = (seconds: number) => `${String(Math.round(seconds / 360) / 10)}h`;

function TrendCharts({ trends }: { trends: Trends }) {
  const labels = trends.points.map((point) => bucketLabel(point.end));
  const at = <T,>(read: (point: Trends["points"][number]) => T) => trends.points.map(read);
  if (trends.points.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        No data in this period.
      </p>
    );
  }
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <LineChart
        title="Open findings"
        labels={labels}
        format={count}
        series={[
          { name: "Critical", values: at((point) => point.findings_open.critical) },
          { name: "High", values: at((point) => point.findings_open.high) },
          { name: "Medium", values: at((point) => point.findings_open.medium) },
          { name: "Low", values: at((point) => point.findings_open.low) },
        ]}
      />
      <LineChart
        title="Rules passing"
        labels={labels}
        format={share}
        max={1}
        series={[{ name: "Rules passing", values: at((point) => point.rule_pass_rate) }]}
      />
      <LineChart
        title="Changes reviewed"
        labels={labels}
        format={share}
        max={1}
        series={[{ name: "Changes reviewed", values: at((point) => point.change_review_rate) }]}
      />
      <LineChart
        title="Time spent"
        labels={labels}
        format={hours}
        series={[{ name: "Time spent", values: at((point) => point.time_spent_seconds) }]}
      />
    </div>
  );
}

function readBucket(value: string | null): Bucket {
  return value === "month" ? "month" : "week";
}

/** Open findings, rules passing, change review and time spent, week by week or month by month. */
export function TrendsPage() {
  const [params, setParams] = useSearchParams();
  const focus = readFocus(params);
  const bucket = readBucket(params.get("bucket"));
  const from = parseDate(params.get("from")) ?? undefined;
  const to = parseDate(params.get("to")) ?? undefined;
  const [problem, setProblem] = useState<string | null>(null);
  const key = `trends?${params.toString()}`;
  const trends = useLoad(key, (signal) => getTrends(focus, bucket, { from, to }, signal));

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const text = (name: string) => {
      const value = form.get(name);
      return typeof value === "string" ? value.trim() : "";
    };
    const nextFrom = parseDate(text("from"));
    const nextTo = parseDate(text("to"));
    if ((text("from") && !nextFrom) || (text("to") && !nextTo)) {
      setProblem("Enter dates as YYYY-MM-DD, or leave them empty for the default period.");
      return;
    }
    if (nextFrom && nextTo && nextFrom > nextTo) {
      setProblem("The start must be on or before the end.");
      return;
    }
    setProblem(null);
    const next = writeFocus(focus);
    const chosen = readBucket(text("bucket"));
    if (chosen === "month") next.set("bucket", "month");
    if (nextFrom) next.set("from", nextFrom);
    if (nextTo) next.set("to", nextTo);
    setParams(next);
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <Header
        title="Trends"
        intro="How posture and change review moved over time, from the evaluation runs and change records in force at the end of each period."
      />
      <FocusPicker />
      <form
        key={params.toString()}
        onSubmit={apply}
        aria-label="Trend period"
        noValidate
        className="grid gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-4 sm:items-end"
      >
        <label className="text-sm font-medium">
          Period
          <select name="bucket" defaultValue={bucket} className={fieldClass}>
            <option value="week">Weekly</option>
            <option value="month">Monthly</option>
          </select>
        </label>
        <label className="text-sm font-medium">
          From
          <input name="from" type="date" defaultValue={from ?? ""} className={fieldClass} />
        </label>
        <label className="text-sm font-medium">
          To
          <input name="to" type="date" defaultValue={to ?? ""} className={fieldClass} />
        </label>
        <button type="submit" className={buttonClass}>
          Show
        </button>
        <p className="text-xs text-slate-600 sm:col-span-4">
          {`Empty dates show the last 12 periods. At most ${String(MAX_BUCKETS.week)} weeks or ${String(MAX_BUCKETS.month)} months.`}
        </p>
      </form>
      {problem && (
        <p role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
          {problem}
        </p>
      )}
      {trends.error?.status === 422 ? (
        <p role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
          {`That period is too long: at most ${String(MAX_BUCKETS.week)} weeks or ${String(MAX_BUCKETS.month)} months.`}
        </p>
      ) : trends.error ? (
        <ErrorMessage error={trends.error} retry={trends.retry} />
      ) : trends.data ? (
        <TrendCharts trends={trends.data} />
      ) : (
        <p role="status" className="text-slate-600">
          Loading trends…
        </p>
      )}
    </section>
  );
}
