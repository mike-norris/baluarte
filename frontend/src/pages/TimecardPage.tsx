import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import type { ApiError } from "../api/client";
import {
  daysAgo,
  DEFAULT_TIMECARD_SORT,
  getTimecard,
  NONE,
  parseDate,
  periodProblem,
  PeopleRefused,
  TIMECARD_COLUMNS,
  timecardUrl,
  type Timecard,
  type TimecardColumn,
  type TimecardQuery,
  type TimecardRow,
  type TimecardSort,
} from "../api/time";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { canReadPeople, readFocus } from "../manager/format";
import { ManagerTabs } from "../manager/parts";
import { formatTime } from "../time";

const DEFAULT_DAYS = 30;
const TEXT_LIMIT = 255;
const SORT_KEYS = 3;

const headings: Record<(typeof TIMECARD_COLUMNS)[number], string> = {
  date: "Date",
  developer: "Developer",
  minutes: "Minutes",
  top: "Project (top-level epic)",
  parent: "Parent",
  work_item: "Issue",
  milestone: "Milestone",
  iteration: "Iteration",
  estimate_minutes: "Estimate (minutes)",
  repository: "Repository",
  entry: "Entry",
};

const NUMERIC: ReadonlySet<TimecardColumn> = new Set(["minutes", "estimate_minutes"]);

type Choice = "person" | "top" | "parent" | "milestone" | "iteration";

function text(value: string | null): string | undefined {
  const trimmed = value?.trim();
  return trimmed && trimmed.length <= TEXT_LIMIT ? trimmed : undefined;
}

function isColumn(value: string): value is TimecardColumn {
  return (
    (TIMECARD_COLUMNS as readonly string[]).includes(value) ||
    ["username", "title", "removed_on"].includes(value)
  );
}

/** Sort keys from the URL; unknown ones are dropped. */
export function parseSort(value: string | null): TimecardSort[] {
  const keys = (value ?? "")
    .split(",")
    .map((key) => key.trim())
    .filter((key) => isColumn(key.replace(/^-/, ""))) as TimecardSort[];
  return keys.length > 0 ? keys.slice(0, SORT_KEYS) : [...DEFAULT_TIMECARD_SORT];
}

/** The timecard query from the URL; the period defaults to the last 30 days. */
export function readTimecardQuery(params: URLSearchParams): TimecardQuery {
  const focus = readFocus(params);
  const query: TimecardQuery = {
    from: parseDate(params.get("from")) ?? daysAgo(DEFAULT_DAYS),
    to: parseDate(params.get("to")) ?? daysAgo(0),
    removed: params.get("removed") !== "false",
    sort: parseSort(params.get("sort")),
  };
  if (focus.group) query.group = focus.group;
  if (focus.tag) query.tag = focus.tag;
  for (const key of ["person", "top", "parent", "milestone", "iteration"] as const) {
    const found = text(params.get(key));
    if (found) query[key] = found;
  }
  return query;
}

export function writeTimecardQuery(query: TimecardQuery): URLSearchParams {
  const params = new URLSearchParams();
  params.set("from", query.from);
  params.set("to", query.to);
  if (query.group) params.set("group", query.group);
  else if (query.tag) params.set("tag", query.tag);
  for (const key of ["person", "top", "parent", "milestone", "iteration"] as const) {
    const value = query[key];
    if (value) params.set(key, value);
  }
  if (!query.removed) params.set("removed", "false");
  const sort = query.sort.join(",");
  if (sort !== DEFAULT_TIMECARD_SORT.join(",")) params.set("sort", sort);
  return params;
}

/** A row's value for a column: what is shown, and what sorting and filtering use. */
export function cellValue(row: TimecardRow, column: TimecardColumn): string | number | null {
  switch (column) {
    case "date":
      return row.spent_on;
    case "top":
      return row.top ? (row.top.title ?? row.top.reference) : null;
    case "parent":
      return row.parent ? (row.parent.title ?? row.parent.reference) : null;
    case "repository":
      return row.project.path;
    case "entry":
      return row.kind === "removed" ? "Removed" : "Logged";
    default:
      return row[column];
  }
}

function compare(a: string | number, b: string | number): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { sensitivity: "base" });
}

/** Rows sorted like the API sorts them: empty values last, a removal after its log. */
export function sortRows(rows: readonly TimecardRow[], sort: readonly TimecardSort[]) {
  return [...rows].sort((a, b) => {
    for (const key of sort) {
      const descending = key.startsWith("-");
      const column = key.replace(/^-/, "") as TimecardColumn;
      const x = cellValue(a, column);
      const y = cellValue(b, column);
      const xEmpty = x === null || x === "";
      const yEmpty = y === null || y === "";
      if (xEmpty || yEmpty) {
        if (xEmpty !== yEmpty) return xEmpty ? 1 : -1;
        continue;
      }
      const order = compare(x, y);
      if (order !== 0) return descending ? -order : order;
    }
    return (
      a.spent_at.localeCompare(b.spent_at) ||
      a.entry_id - b.entry_id ||
      Number(a.kind === "removed") - Number(b.kind === "removed")
    );
  });
}

/** Clicking a column sorts by it first; clicking it again reverses it. */
export function nextSort(sort: readonly TimecardSort[], column: TimecardColumn): TimecardSort[] {
  const first = sort[0];
  const head: TimecardSort = first === column ? `-${column}` : column;
  const rest = sort.filter((key) => key.replace(/^-/, "") !== column);
  return [head, ...rest].slice(0, SORT_KEYS);
}

export function formatMinutes(minutes: number): string {
  return Number.isInteger(minutes) ? String(minutes) : minutes.toFixed(2).replace(/0$/, "");
}

function hours(minutes: number): string {
  const sign = minutes < 0 ? "-" : "";
  const whole = Math.round(Math.abs(minutes));
  const h = Math.floor(whole / 60);
  const m = whole % 60;
  return `${sign}${String(h)}h ${String(m)}m`;
}

function options(rows: readonly TimecardRow[], pick: (row: TimecardRow) => string | null) {
  const found = new Set<string>();
  let empty = false;
  for (const row of rows) {
    const value = pick(row);
    if (value) found.add(value);
    else empty = true;
  }
  const sorted = [...found].sort((a, b) => a.localeCompare(b));
  return empty ? [...sorted, NONE] : sorted;
}

function Filters({ query, card }: { query: TimecardQuery; card: Timecard | null }) {
  const [, setParams] = useSearchParams();
  const [problem, setProblem] = useState<string | null>(null);
  const rows = card?.rows ?? [];
  const people = new Map<string, string>();
  for (const row of rows) if (row.username) people.set(row.username, row.developer);
  if (query.person && !people.has(query.person)) people.set(query.person, query.person);
  const choices: {
    key: Exclude<Choice, "person">;
    label: string;
    values: string[];
  }[] = [
    {
      key: "top",
      label: "Project (top-level epic)",
      values: options(rows, (r) => cellValue(r, "top") as string | null),
    },
    {
      key: "parent",
      label: "Parent",
      values: options(rows, (r) => cellValue(r, "parent") as string | null),
    },
    {
      key: "milestone",
      label: "Milestone",
      values: options(rows, (r) => r.milestone),
    },
    {
      key: "iteration",
      label: "Iteration",
      values: options(rows, (r) => r.iteration),
    },
  ];
  for (const choice of choices) {
    const current = query[choice.key];
    if (current && !choice.values.includes(current)) choice.values.push(current);
  }

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = (name: string) => {
      const found = form.get(name);
      return typeof found === "string" && found !== "" ? found : undefined;
    };
    const from = value("from") ?? "";
    const to = value("to") ?? "";
    const found = periodProblem(from, to);
    setProblem(found);
    if (found !== null) return;
    const next: TimecardQuery = {
      from,
      to,
      removed: form.get("removed") === "on",
      sort: query.sort,
    };
    if (query.group) next.group = query.group;
    if (query.tag) next.tag = query.tag;
    for (const key of ["person", "top", "parent", "milestone", "iteration"] as const) {
      const found = value(key);
      if (found) next[key] = found;
    }
    setParams(writeTimecardQuery(next));
  }

  return (
    <form
      key={writeTimecardQuery(query).toString()}
      onSubmit={apply}
      aria-label="Timecard filters"
      className="grid gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-2 lg:grid-cols-4"
    >
      <label className="text-sm font-medium">
        From
        <input name="from" type="date" required defaultValue={query.from} className={fieldClass} />
      </label>
      <label className="text-sm font-medium">
        To
        <input name="to" type="date" required defaultValue={query.to} className={fieldClass} />
      </label>
      <label className="text-sm font-medium">
        Developer
        <select name="person" defaultValue={query.person ?? ""} className={fieldClass}>
          <option value="">All developers</option>
          {[...people.entries()]
            .sort((a, b) => a[1].localeCompare(b[1]))
            .map(([username, name]) => (
              <option key={username} value={username}>
                {name === username ? username : `${name} (${username})`}
              </option>
            ))}
        </select>
      </label>
      {choices.map((choice) => (
        <label key={choice.key} className="text-sm font-medium">
          {choice.label}
          <select name={choice.key} defaultValue={query[choice.key] ?? ""} className={fieldClass}>
            <option value="">All</option>
            {choice.values.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      ))}
      <label className="flex items-center gap-2 self-end text-sm">
        <input name="removed" type="checkbox" defaultChecked={query.removed} />
        Include time removed in GitLab
      </label>
      {problem && (
        <p role="alert" className="text-sm text-red-800 sm:col-span-2 lg:col-span-4">
          {problem}
        </p>
      )}
      <div className="flex items-end gap-2 sm:col-span-2 lg:col-span-4">
        <button type="submit" className={buttonClass}>
          Show timecard
        </button>
        <button
          type="button"
          className={secondaryClass}
          onClick={() => {
            const reset: TimecardQuery = {
              from: query.from,
              to: query.to,
              removed: true,
              sort: query.sort,
            };
            if (query.group) reset.group = query.group;
            if (query.tag) reset.tag = query.tag;
            setParams(writeTimecardQuery(reset));
          }}
        >
          Clear filters
        </button>
      </div>
    </form>
  );
}

function Cell({ row, column }: { row: TimecardRow; column: (typeof TIMECARD_COLUMNS)[number] }) {
  if (column === "minutes") {
    return (
      <td className={`px-3 py-2 text-right tabular-nums ${row.minutes < 0 ? "text-red-800" : ""}`}>
        {formatMinutes(row.minutes)}
      </td>
    );
  }
  if (column === "estimate_minutes") {
    return (
      <td className="px-3 py-2 text-right tabular-nums">
        {row.estimate_minutes === null ? "" : formatMinutes(row.estimate_minutes)}
      </td>
    );
  }
  if (column === "work_item") {
    const label = row.work_item ?? "";
    return (
      <td className="px-3 py-2">
        {row.url ? (
          <a href={row.url} className={linkClass} rel="noreferrer" target="_blank">
            {label}
          </a>
        ) : (
          label
        )}
        {row.title && <span className="block text-xs text-slate-600">{row.title}</span>}
      </td>
    );
  }
  if (column === "entry") {
    return (
      <td className="px-3 py-2">
        {row.kind === "removed" ? (
          <span>{`Removed in GitLab${row.removed_on ? ` (noticed ${row.removed_on})` : ""}`}</span>
        ) : (
          "Logged"
        )}
      </td>
    );
  }
  const value = cellValue(row, column);
  return <td className="px-3 py-2">{value === null ? "" : String(value)}</td>;
}

function TimecardTable({ card, query }: { card: Timecard; query: TimecardQuery }) {
  const [, setParams] = useSearchParams();
  const rows = sortRows(card.rows, query.sort);
  const first = query.sort[0] ?? "";
  if (rows.length === 0) {
    return <p className="text-sm text-slate-600">No time logged with these filters.</p>;
  }
  return (
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Timecard rows, sortable by column</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            {TIMECARD_COLUMNS.map((column) => {
              const sorted =
                first === column ? "ascending" : first === `-${column}` ? "descending" : undefined;
              return (
                <th
                  key={column}
                  scope="col"
                  aria-sort={sorted}
                  className={`px-3 py-2 font-medium ${NUMERIC.has(column) ? "text-right" : ""}`}
                >
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
                    onClick={() =>
                      setParams(
                        writeTimecardQuery({
                          ...query,
                          sort: nextSort(query.sort, column),
                        }),
                      )
                    }
                  >
                    {headings[column]}
                    <span aria-hidden="true">
                      {sorted === "ascending" ? "▲" : sorted === "descending" ? "▼" : ""}
                    </span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row) => (
            <tr
              key={`${String(row.entry_id)}-${row.kind}`}
              className={row.kind === "removed" ? "bg-red-50" : undefined}
            >
              {TIMECARD_COLUMNS.map((column) => (
                <Cell key={column} row={row} column={column} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Totals({ card }: { card: Timecard }) {
  const { totals } = card;
  return (
    <div className="space-y-3">
      <dl className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {[
          ["Total", `${formatMinutes(totals.minutes)} min (${hours(totals.minutes)})`],
          ["Logged", `${formatMinutes(totals.logged_minutes)} min`],
          ["Removed in GitLab", `${formatMinutes(totals.removed_minutes)} min`],
          ["Rows", String(totals.rows)],
          ["Developers", String(totals.people)],
        ].map(([term, value]) => (
          <div key={term} className="rounded border border-slate-200 bg-white p-3">
            <dt className="text-xs text-slate-600">{term}</dt>
            <dd className="text-lg font-semibold tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      {Object.keys(card.per_person).length > 1 && (
        <table className="text-left text-sm">
          <caption className="text-left font-medium">Minutes per developer</caption>
          <tbody>
            {Object.entries(card.per_person).map(([name, minutes]) => (
              <tr key={name}>
                <th scope="row" className="pr-4 font-normal">
                  {name}
                </th>
                <td className="text-right tabular-nums">{formatMinutes(minutes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Refusal({ error, retry }: { error: ApiError; retry: () => void }) {
  if (error instanceof PeopleRefused) {
    return (
      <p
        role="alert"
        className="rounded border border-slate-300 bg-slate-50 p-3 text-sm text-slate-800"
      >
        {error.message}
      </p>
    );
  }
  return <ErrorMessage error={error} retry={retry} />;
}

function TimecardBody({ query }: { query: TimecardQuery }) {
  // Sorting happens in the page, so changing it does not read (and audit) the timecard again.
  const fetched = { ...query, sort: DEFAULT_TIMECARD_SORT };
  const key = writeTimecardQuery(fetched).toString();
  const card = useLoad(`timecard?${key}`, (signal) => getTimecard(fetched, signal));
  return (
    <>
      <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
        Timecards are personal data. Every view and download is recorded in Baluarte&apos;s audit
        log with the filters used, and they cover only the groups assigned to you.
      </p>
      <Filters query={query} card={card.data ?? null} />
      <div className="flex flex-wrap gap-2">
        <a href={timecardUrl(query, "csv")} download className={secondaryClass}>
          Download CSV
        </a>
        <a href={timecardUrl(query, "html")} download className={secondaryClass}>
          Download HTML
        </a>
      </div>
      {card.error ? (
        <Refusal error={card.error} retry={card.retry} />
      ) : card.data ? (
        <>
          <p className="text-sm text-slate-600">
            {card.data.collected_at
              ? `Time logs last read ${formatTime(card.data.collected_at)}.`
              : "Time logs have not been read yet."}
          </p>
          <Totals card={card.data} />
          <TimecardTable card={card.data} query={query} />
        </>
      ) : (
        <p role="status" className="text-sm text-slate-600">
          Loading the timecard…
        </p>
      )}
    </>
  );
}

/** One row per time log with its developer and the issue's planning context. */
export function TimecardPage() {
  const [params] = useSearchParams();
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const query = readTimecardQuery(params);

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div className="space-y-3">
        <div>
          <h1 id="page-title" className="text-2xl font-semibold">
            Timecard
          </h1>
          <p className="mt-2 text-slate-600">
            Each time log of the period with its developer and the issue&apos;s milestone,
            iteration, parent and top-level epic. Time removed in GitLab shows as a negative row on
            the same date, so the totals balance.
          </p>
        </div>
        <ManagerTabs />
      </div>
      {canReadPeople(user) ? (
        <TimecardBody query={query} />
      ) : (
        <p role="alert" className="text-sm text-slate-700">
          You do not have permission to see time per person.
        </p>
      )}
    </section>
  );
}
