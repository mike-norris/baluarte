import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import {
  exceptionStates,
  listExceptions,
  type ExceptionFilters,
  type ExceptionPage,
  type PolicyException,
} from "../api/exceptions";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import {
  exceptionStateLabels,
  readExceptionFilters,
  requesterName,
  writeExceptionFilters,
} from "../exceptions/format";
import { formatTime } from "../time";

const cell = "px-3 py-2";

function Filters({ filters }: { filters: ExceptionFilters }) {
  const [, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const query = new URLSearchParams();
    for (const field of ["state", "project", "policy", "rule"]) {
      const value = form.get(field);
      if (typeof value === "string" && value.trim()) query.set(field, value.trim());
    }
    setParams(writeExceptionFilters(readExceptionFilters(query)));
  }

  const active = writeExceptionFilters(filters).size > 0;
  return (
    <form
      key={writeExceptionFilters(filters).toString()}
      onSubmit={apply}
      aria-label="Filter exceptions"
      className="grid gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-2 lg:grid-cols-4 lg:items-end"
    >
      <label className="text-sm font-medium">
        State
        <select name="state" defaultValue={filters.state ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {exceptionStates.map((state) => (
            <option key={state} value={state}>
              {exceptionStateLabels[state]}
            </option>
          ))}
        </select>
      </label>
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
        Policy name
        <input
          name="policy"
          defaultValue={filters.policy ?? ""}
          maxLength={63}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        Rule ID
        <input
          name="rule"
          defaultValue={filters.rule ?? ""}
          maxLength={63}
          className={fieldClass}
        />
      </label>
      <div className="flex gap-2 sm:col-span-2 lg:col-span-4">
        <button type="submit" className={buttonClass}>
          Apply filters
        </button>
        {active && (
          <Link to="/exceptions" className={secondaryClass}>
            Clear
          </Link>
        )}
      </div>
    </form>
  );
}

function StateCell({ exception }: { exception: PolicyException }) {
  return (
    <td className={cell}>
      {exceptionStateLabels[exception.state]}
      {exception.in_force && <span className="ml-1 text-slate-600">(in force)</span>}
    </td>
  );
}

const headers = ["Exception", "Project", "Policy and rule", "State", "Requested by", "Expires"];

function Listing({ filters }: { filters: ExceptionFilters }) {
  const key = writeExceptionFilters(filters).toString();
  const first = useLoad(`exceptions?${key}`, (signal) => listExceptions(filters, null, signal));
  const paged = usePaged<PolicyException, ExceptionPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listExceptions(filters, after),
  );

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (first.loading) {
    return (
      <p role="status" className="text-slate-600">
        Loading exceptions…
      </p>
    );
  }
  if (paged.items.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {key ? "No exceptions match these filters." : "No exceptions have been requested."}
      </p>
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Policy exceptions</caption>
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
            {paged.items.map((exception) => (
              <tr key={exception.id}>
                <th scope="row" className={`${cell} font-medium`}>
                  <Link to={`/exceptions/${String(exception.id)}`} className={linkClass}>
                    {`Exception ${String(exception.id)}`}
                  </Link>
                </th>
                <td className={cell}>{exception.project.path}</td>
                <td className={cell}>{`${exception.policy_name} / ${exception.rule_id}`}</td>
                <StateCell exception={exception} />
                <td className={cell}>{requesterName(exception)}</td>
                <td className={`${cell} whitespace-nowrap`}>{formatTime(exception.expires_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-sm text-slate-600" aria-live="polite">
        {`Showing ${String(paged.items.length)} exceptions${more}.`}
      </p>
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
          {paged.pending ? "Loading…" : "Load more exceptions"}
        </button>
      )}
    </div>
  );
}

export function ExceptionsPage() {
  const [params] = useSearchParams();
  const filters = readExceptionFilters(params);

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Exceptions
        </h1>
        <p className="mt-2 text-slate-600">
          Time-limited waivers of one rule for one project. An approved exception records a known
          failure as excepted, never as passing, and never covers unknown or unsupported results.
          Someone other than the requester must approve it, and every step is audited. "Expired"
          includes any exception past its expiry, even before the next evaluation records it.
        </p>
      </div>
      <Filters filters={filters} />
      <Listing filters={filters} />
    </section>
  );
}
