import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import {
  changeHref,
  changeKinds,
  changeOutcomes,
  CHANGES,
  listChanges,
  type Change,
  type ChangeFilters,
  type ChangePage,
  type ChangeKind,
  type ChangeOutcome,
} from "../api/changes";
import { projectHref } from "../api/gitlab";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { toLocal } from "../asof/format";
import {
  approvalsPart,
  changeName,
  changeOutcomeLabels,
  deploymentPart,
  formFilters,
  hasChangeFilters,
  kindLabels,
  pipelinePart,
  readChangeFilters,
  writeChangeFilters,
} from "../changes/format";
import { PartText } from "../changes/PartText";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { formatTime } from "../time";

const cell = "px-3 py-2 align-top";

const outcomeClass: Record<ChangeOutcome, string> = {
  pass: "bg-emerald-100 text-emerald-900",
  fail: "bg-red-100 text-red-900",
  unknown: "bg-amber-100 text-amber-900",
};

function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

function Filters({ filters }: { filters: ChangeFilters }) {
  const [params, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = writeChangeFilters(
      formFilters({
        project: text(form, "project"),
        kind: changeKinds.find((kind) => kind === text(form, "kind")) ?? "",
        from: text(form, "from"),
        to: text(form, "to"),
        outcome: changeOutcomes.find((outcome) => outcome === text(form, "outcome")) ?? "",
      }),
    );
    const asOf = params.get("as_of");
    if (asOf) next.set("as_of", asOf);
    setParams(next);
  }

  return (
    <form
      key={writeChangeFilters(filters).toString()}
      onSubmit={apply}
      aria-label="Filter changes"
      className="grid gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-2 lg:grid-cols-5 lg:items-end"
    >
      <label className="text-sm font-medium">
        Project GitLab ID
        <input
          name="project"
          type="number"
          min={1}
          step={1}
          defaultValue={filters.project ?? ""}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        Kind
        <select name="kind" defaultValue={filters.kind ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {changeKinds.map((kind: ChangeKind) => (
            <option key={kind} value={kind}>
              {kindLabels[kind]}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm font-medium">
        From (UTC)
        <input
          name="from"
          type="datetime-local"
          defaultValue={filters.from ? toLocal(filters.from) : ""}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        Before (UTC)
        <input
          name="to"
          type="datetime-local"
          defaultValue={filters.to ? toLocal(filters.to) : ""}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        Status
        <select name="outcome" defaultValue={filters.outcome ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {changeOutcomes.map((outcome) => (
            <option key={outcome} value={outcome}>
              {changeOutcomeLabels[outcome]}
            </option>
          ))}
        </select>
      </label>
      <div className="flex gap-2 sm:col-span-2 lg:col-span-5">
        <button type="submit" className={buttonClass}>
          Apply filters
        </button>
        {hasChangeFilters(filters) && (
          <Link to={CHANGES} className={secondaryClass}>
            Clear
          </Link>
        )}
      </div>
    </form>
  );
}

function ChangeRow({ change }: { change: Change }) {
  return (
    <tr>
      <td className={cell}>
        <Link to={projectHref(change.project.gitlab_id)} className={linkClass}>
          {change.project.path}
        </Link>
      </td>
      <th scope="row" className={`${cell} font-normal`}>
        <Link to={changeHref(change.id)} className={`${linkClass} font-medium`}>
          {changeName(change)}
        </Link>
        <span className="block text-xs text-slate-600">{kindLabels[change.kind]}</span>
        {change.complete === false && (
          <span className="block text-xs text-amber-900">Partly read; it will be read again</span>
        )}
      </th>
      <td className={cell}>{change.author ?? "Unknown"}</td>
      <td className={`${cell} whitespace-nowrap`}>{formatTime(change.changed_at)}</td>
      <td className={cell}>
        <PartText part={approvalsPart(change.approvals)} />
      </td>
      <td className={cell}>
        <PartText part={pipelinePart(change.pipeline)} />
      </td>
      <td className={cell}>
        <PartText part={deploymentPart(change.deployments)} />
      </td>
      <td className={cell}>
        <span
          className={`rounded px-2 py-0.5 text-xs font-medium ${outcomeClass[change.outcome] ?? ""}`}
        >
          {changeOutcomeLabels[change.outcome] ?? change.outcome}
        </span>
        {change.reasons.map((reason) => (
          <span key={reason} className="mt-1 block text-xs text-slate-700">
            {reason}
          </span>
        ))}
      </td>
    </tr>
  );
}

const headers = [
  "Project",
  "Change",
  "Author",
  "Merged or pushed",
  "Approvals",
  "Pipeline and tests",
  "Deployment",
  "Status",
];

function Listing({ filters }: { filters: ChangeFilters }) {
  const asOf = useAsOf();
  const key = `${writeChangeFilters(filters).toString()}@${asOf ?? "now"}`;
  const first = useLoad(`changes?${key}`, (signal) => listChanges(filters, null, asOf, signal));
  const paged = usePaged<Change, ChangePage>(
    key,
    first,
    (page) => page.next_before_id,
    (before) => listChanges(filters, before, asOf),
  );

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (first.loading) {
    return (
      <p role="status" className="text-slate-600">
        Loading changes…
      </p>
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <div className="space-y-3">
      <AsOfResult answer={first.data} />
      {paged.items.length === 0 ? (
        <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {hasChangeFilters(filters)
            ? "No changes match these filters."
            : "No changes recorded yet. Changes are read from each monitored project's default branch, from the first collection back over the configured lookback."}
        </p>
      ) : (
        <>
          <div className="overflow-x-auto rounded border border-slate-200 bg-white">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Changes, newest first</caption>
              <thead className="border-b border-slate-200 bg-slate-50">
                <tr>
                  {headers.map((header) => (
                    <th key={header} scope="col" className={`${cell} font-medium`}>
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {paged.items.map((change) => (
                  <ChangeRow key={change.id} change={change} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-sm text-slate-600" aria-live="polite">
            {`Showing ${String(paged.items.length)} changes${more}.`}
          </p>
        </>
      )}
      {paged.moreError && (
        <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
      )}
      {paged.next !== null && (
        <button
          type="button"
          onClick={() => void paged.loadMore()}
          disabled={paged.pending}
          className={secondaryClass}
        >
          {paged.pending ? "Loading…" : "Load older changes"}
        </button>
      )}
    </div>
  );
}

export function ChangesPage() {
  const [params] = useSearchParams();
  const filters = readChangeFilters(params);
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Changes
        </h1>
        <p className="mt-2 max-w-3xl text-slate-600">
          Merge requests merged into each monitored project’s default branch, and commits pushed to
          it directly, with who approved them, their pipeline and tests, and where they were
          deployed. Anything that could not be read is unknown, never counted as reviewed or passed.
        </p>
      </div>
      <Filters filters={filters} />
      <Listing filters={filters} />
    </section>
  );
}
