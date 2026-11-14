import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import type { ApiError } from "../api/client";
import { listTags, parseTagParam, tagParam, tagText } from "../api/tags";
import {
  againstEstimate,
  daysAgo,
  formatDuration,
  getPeople,
  getPersonEntries,
  getTimeSummary,
  parseDate,
  periodProblem,
  PeopleRefused,
  type PeopleSummary,
  type PersonEntries as PersonEntriesPage,
  type TimeEntry,
  type TimeFilters,
  type TimeGrouping,
  type TimeSummary,
} from "../api/time";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { canReadPeople, MANAGER_TIME, readFocus } from "../manager/format";
import { ManagerTabs } from "../manager/parts";
import { formatTime } from "../time";
import { PageHeader } from "../ui/PageHeader";

const DEFAULT_DAYS = 30;
const LABEL_LIMIT = 255;

const groupings: { value: string; label: string }[] = [
  { value: "group", label: "Group" },
  { value: "project", label: "Project" },
  { value: "label", label: "Label" },
  { value: "milestone", label: "Milestone" },
  { value: "week", label: "Week" },
];

function text(value: string | null): string | undefined {
  const trimmed = value?.trim();
  return trimmed && trimmed.length <= LABEL_LIMIT ? trimmed : undefined;
}

/** Filters and grouping from the URL; the period defaults to the last 30 days. */
export function readTimeView(params: URLSearchParams): {
  filters: TimeFilters;
  by: TimeGrouping;
} {
  const focus = readFocus(params);
  const filters: TimeFilters = {
    from: parseDate(params.get("from")) ?? daysAgo(DEFAULT_DAYS),
    to: parseDate(params.get("to")) ?? daysAgo(0),
  };
  if (focus.group) filters.group = focus.group;
  if (focus.tag) filters.tag = focus.tag;
  const project = Number(params.get("project"));
  if (Number.isSafeInteger(project) && project > 0) filters.project = project;
  const label = text(params.get("label"));
  if (label) filters.label = label;
  const milestone = text(params.get("milestone"));
  if (milestone) filters.milestone = milestone;
  const raw = params.get("by") ?? "group";
  const byTag = raw.startsWith("tag:") && parseTagParam(`${raw.slice(4)}:x`) !== null;
  const by: TimeGrouping = groupings.some((item) => item.value === raw)
    ? (raw as TimeGrouping)
    : byTag
      ? (raw as TimeGrouping)
      : "group";
  return { filters, by };
}

function writeTimeView(filters: TimeFilters, by: string, person?: string): URLSearchParams {
  const params = new URLSearchParams();
  params.set("from", filters.from);
  params.set("to", filters.to);
  if (filters.group) params.set("group", filters.group);
  else if (filters.tag) params.set("tag", filters.tag);
  if (filters.project !== undefined) params.set("project", String(filters.project));
  if (filters.label) params.set("label", filters.label);
  if (filters.milestone) params.set("milestone", filters.milestone);
  if (by !== "group") params.set("by", by);
  if (person) params.set("person", person);
  return params;
}

function Filters({ filters, by }: { filters: TimeFilters; by: TimeGrouping }) {
  const [, setParams] = useSearchParams();
  const [problem, setProblem] = useState<string | null>(null);
  const tags = useLoad("tags", (signal) => listTags(signal));
  const tagKeys = [...new Set((tags.data?.items ?? []).map((tag) => tag.key))];

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = (name: string) => {
      const entry = form.get(name);
      return typeof entry === "string" ? entry.trim() : "";
    };
    const found = periodProblem(value("from"), value("to"));
    setProblem(found);
    if (found) return;
    const next: TimeFilters = { from: value("from"), to: value("to") };
    const group = value("group");
    if (group) next.group = group;
    const tag = value("tag");
    if (!group && tag) next.tag = tag;
    if (value("label")) next.label = value("label");
    if (value("milestone")) next.milestone = value("milestone");
    if (filters.project !== undefined) next.project = filters.project;
    setParams(writeTimeView(next, value("by") || "group"));
  }

  return (
    <form
      key={writeTimeView(filters, by).toString()}
      onSubmit={apply}
      aria-label="Filter time"
      noValidate
      className="grid gap-3 rounded border p-3 sm:grid-cols-2 lg:grid-cols-4 lg:items-end"
    >
      {problem && (
        <p role="alert" className="text-danger-emphasis sm:col-span-2 lg:col-span-4">
          {problem}
        </p>
      )}
      <label className="fw-medium">
        From
        <input name="from" type="date" defaultValue={filters.from} className="form-control mt-1" />
      </label>
      <label className="fw-medium">
        To
        <input name="to" type="date" defaultValue={filters.to} className="form-control mt-1" />
      </label>
      <label className="fw-medium">
        Group path
        <input
          name="group"
          defaultValue={filters.group ?? ""}
          maxLength={LABEL_LIMIT}
          placeholder="group/subgroup"
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        Tag
        <select name="tag" defaultValue={filters.tag ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {(tags.data?.items ?? []).map((tag) => (
            <option key={tag.id} value={tagParam(tag)}>
              {tagText(tag)}
            </option>
          ))}
        </select>
      </label>
      <label className="fw-medium">
        Label
        <input
          name="label"
          defaultValue={filters.label ?? ""}
          maxLength={LABEL_LIMIT}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        Milestone
        <input
          name="milestone"
          defaultValue={filters.milestone ?? ""}
          maxLength={LABEL_LIMIT}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        Group by
        <select name="by" defaultValue={by} className="form-select mt-1">
          {groupings.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
          {tagKeys.map((key) => (
            <option key={key} value={`tag:${key}`}>
              {`Tag ${key}`}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" className="btn btn-primary">
        Apply
      </button>
    </form>
  );
}

function groupingLabel(by: string): string {
  if (by.startsWith("tag:")) return `Tag ${by.slice(4)}`;
  return groupings.find((item) => item.value === by)?.label ?? by;
}

function SummaryTable({ summary }: { summary: TimeSummary }) {
  const heading = groupingLabel(summary.by);
  if (summary.rows.length === 0) {
    return (
      <p className="text-secondary border border-dashed rounded p-3">
        No time was logged in this period.
      </p>
    );
  }
  return (
    <div className="card table-responsive">
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">{`Time by ${heading.toLowerCase()}`}</caption>
        <thead>
          <tr>
            {[heading, "Spent", "Estimated", "Against estimate", "Issues", "Entries"].map(
              (title) => (
                <th key={title} scope="col" className="px-3 py-2 fw-medium">
                  {title}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {summary.rows.map((row) => (
            <tr key={row.key}>
              <th scope="row" className="px-3 py-2 fw-medium">
                {row.label}
              </th>
              <td className="px-3 py-2">{formatDuration(row.spent_seconds)}</td>
              <td className="px-3 py-2">{formatDuration(row.estimate_seconds)}</td>
              <td className="px-3 py-2">
                {againstEstimate(row.spent_seconds, row.estimate_seconds)}
              </td>
              <td className="px-3 py-2">{row.issues}</td>
              <td className="px-3 py-2">{row.entries}</td>
            </tr>
          ))}
        </tbody>
        <tfoot className="alert alert-secondary text-body">
          <tr>
            <th scope="row" className="px-3 py-2 fw-medium">
              Total
            </th>
            <td className="px-3 py-2 fw-medium">{formatDuration(summary.totals.spent_seconds)}</td>
            <td className="px-3 py-2">{formatDuration(summary.totals.estimate_seconds)}</td>
            <td className="px-3 py-2">
              {againstEstimate(summary.totals.spent_seconds, summary.totals.estimate_seconds)}
            </td>
            <td className="px-3 py-2">{summary.totals.issues}</td>
            <td className="px-3 py-2">{summary.totals.entries}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function AuditedNote() {
  return (
    <p className="alert alert-warning text-body">
      Time per person is personal data. Every view of it is recorded in Baluarte&apos;s audit log
      with the filters used, and it is shown only for the groups assigned to you.
    </p>
  );
}

function Refusal({ error, retry }: { error: ApiError; retry: () => void }) {
  if (error instanceof PeopleRefused) {
    return (
      <p role="alert" className="alert alert-secondary text-body">
        {error.message}
      </p>
    );
  }
  return <ErrorMessage error={error} retry={retry} />;
}

function PeopleTable({
  people,
  filters,
  by,
}: {
  people: PeopleSummary;
  filters: TimeFilters;
  by: string;
}) {
  if (people.rows.length === 0) {
    return <p className="text-secondary">No one logged time in this period.</p>;
  }
  return (
    <div className="card table-responsive">
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">Time per person</caption>
        <thead>
          <tr>
            {["Person", "Spent", "Issues", "Entries"].map((title) => (
              <th key={title} scope="col" className="px-3 py-2 fw-medium">
                {title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {people.rows.map((row) => (
            <tr key={row.username}>
              <th scope="row" className="px-3 py-2 fw-medium">
                <Link to={`${MANAGER_TIME}?${writeTimeView(filters, by, row.username).toString()}`}>
                  {row.username}
                </Link>
              </th>
              <td className="px-3 py-2">{formatDuration(row.spent_seconds)}</td>
              <td className="px-3 py-2">{row.issues}</td>
              <td className="px-3 py-2">{row.entries}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Totals per person, loaded only when asked for, because each view is audited. */
function People({ filters, by }: { filters: TimeFilters; by: TimeGrouping }) {
  const [shown, setShown] = useState<string | null>(null);
  const key = writeTimeView(filters, by).toString();
  if (shown !== key) {
    return (
      <div>
        <AuditedNote />
        <button
          type="button"
          onClick={() => {
            setShown(key);
          }}
          className="btn"
        >
          Show time per person
        </button>
      </div>
    );
  }
  return <PeopleLoaded filters={filters} by={by} />;
}

function PeopleLoaded({ filters, by }: { filters: TimeFilters; by: TimeGrouping }) {
  const people = useLoad(`people?${writeTimeView(filters, by).toString()}`, (signal) =>
    getPeople(filters, signal),
  );
  return (
    <div>
      <AuditedNote />
      {people.error ? (
        <Refusal error={people.error} retry={people.retry} />
      ) : people.data ? (
        <PeopleTable people={people.data} filters={filters} by={by} />
      ) : (
        <p role="status" className="text-secondary">
          Loading time per person…
        </p>
      )}
    </div>
  );
}

function PersonEntries({
  username,
  filters,
  by,
}: {
  username: string;
  filters: TimeFilters;
  by: TimeGrouping;
}) {
  const range = { from: filters.from, to: filters.to };
  const key = `person-${username}?${range.from}..${range.to}`;
  const first = useLoad(key, (signal) => getPersonEntries(username, range, null, signal));
  const paged = usePaged<TimeEntry, PersonEntriesPage, string>(
    key,
    first,
    (page) => page.next_after,
    (after) => getPersonEntries(username, range, after),
  );
  return (
    <section aria-labelledby="person-title">
      <h2 id="person-title" className="card-title">
        {`Time logged by ${username}`}
      </h2>
      <p>
        <Link to={`${MANAGER_TIME}?${writeTimeView(filters, by).toString()}`}>
          Back to the summary
        </Link>
      </p>
      <AuditedNote />
      {first.error ? (
        <Refusal error={first.error} retry={first.retry} />
      ) : first.loading ? (
        <p role="status" className="text-secondary">
          Loading entries…
        </p>
      ) : paged.items.length === 0 ? (
        <p className="text-secondary">No time logged in this period.</p>
      ) : (
        <div className="card table-responsive">
          <table className="table table-vcenter card-table mb-0">
            <caption className="visually-hidden">{`Time entries of ${username}`}</caption>
            <thead>
              <tr>
                {["Spent at", "Time", "Project", "Item", "Labels", "Milestone"].map((title) => (
                  <th key={title} scope="col" className="px-3 py-2 fw-medium">
                    {title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paged.items.map((entry) => (
                <tr key={entry.id}>
                  <td className="px-3 py-2 text-nowrap">
                    {formatTime(entry.spent_at)}
                    {entry.removed_at && (
                      <span className="d-block small text-secondary">
                        {`Removed in GitLab, noticed ${formatTime(entry.removed_at)}`}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">{formatDuration(entry.seconds)}</td>
                  <td className="px-3 py-2">{entry.project.path}</td>
                  <td className="px-3 py-2">
                    {entry.issue_iid !== null
                      ? `Issue #${String(entry.issue_iid)}`
                      : entry.merge_request_iid !== null
                        ? `Merge request !${String(entry.merge_request_iid)}`
                        : "None"}
                  </td>
                  <td className="px-3 py-2">
                    {entry.labels.length > 0 ? entry.labels.join(", ") : "None"}
                  </td>
                  <td className="px-3 py-2">{entry.milestone ?? "None"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {paged.moreError && (
        <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
      )}
      {paged.next !== null && (
        <button
          type="button"
          onClick={() => void paged.loadMore()}
          disabled={paged.pending}
          className="btn"
        >
          {paged.pending ? "Loading…" : "Load more entries"}
        </button>
      )}
    </section>
  );
}

/** Time logged in GitLab, summed by group, project, label, milestone, week or tag. */
export function TimePage() {
  const [params] = useSearchParams();
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const { filters, by } = readTimeView(params);
  const person = text(params.get("person"));
  const key = writeTimeView(filters, by).toString();
  const summary = useLoad(`time?${key}`, (signal) => getTimeSummary(filters, by, signal));
  const peopleAllowed = canReadPeople(user) && summary.data?.per_person_enabled === true;

  return (
    <section aria-labelledby="page-title">
      <div>
        <div>
          <PageHeader
            pretitle="Manager"
            title="Time"
            subtitle="Time logged and estimated on GitLab issues and merge requests. Time is never a finding or evidence of a control."
          />
        </div>
        <ManagerTabs />
      </div>
      <Filters filters={filters} by={by} />
      {summary.error ? (
        <ErrorMessage error={summary.error} retry={summary.retry} />
      ) : summary.data ? (
        <>
          <p className="text-secondary">
            {summary.data.collected_at
              ? `Time logs last read ${formatTime(summary.data.collected_at)}.`
              : "Time logs have not been read yet."}
          </p>
          {person && peopleAllowed ? (
            <PersonEntries username={person} filters={filters} by={by} />
          ) : (
            <SummaryTable summary={summary.data} />
          )}
          {!person && peopleAllowed && (
            <section aria-labelledby="people-title">
              <h2 id="people-title" className="card-title">
                Time per person
              </h2>
              <People filters={filters} by={by} />
            </section>
          )}
          {canReadPeople(user) && !summary.data.per_person_enabled && (
            <p className="text-secondary">This deployment does not show time per person.</p>
          )}
        </>
      ) : (
        <p role="status" className="text-secondary">
          Loading time…
        </p>
      )}
    </section>
  );
}
