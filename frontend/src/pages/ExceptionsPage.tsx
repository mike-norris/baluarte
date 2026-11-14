import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import {
  exceptionStates,
  listExceptions,
  type ExceptionFilters,
  type ExceptionPage,
  type ExceptionState,
  type PolicyException,
} from "../api/exceptions";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  exceptionStateLabels,
  readExceptionFilters,
  requesterName,
  writeExceptionFilters,
} from "../exceptions/format";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

const stateTone: Record<ExceptionState, Tone> = {
  requested: "info",
  approved: "success",
  rejected: "danger",
  revoked: "secondary",
  expired: "secondary",
};

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
      className="card-body border-bottom row g-2 align-items-end mx-0"
    >
      <label className="form-label col-sm-6 col-lg-3 mb-0">
        State
        <select name="state" defaultValue={filters.state ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {exceptionStates.map((state) => (
            <option key={state} value={state}>
              {exceptionStateLabels[state]}
            </option>
          ))}
        </select>
      </label>
      <label className="form-label col-sm-6 col-lg-3 mb-0">
        Project GitLab ID
        <input
          name="project"
          type="number"
          min={1}
          step={1}
          defaultValue={filters.project ?? ""}
          className="form-control mt-1"
        />
      </label>
      <label className="form-label col-sm-6 col-lg-3 mb-0">
        Policy name
        <input
          name="policy"
          defaultValue={filters.policy ?? ""}
          maxLength={63}
          className="form-control mt-1"
        />
      </label>
      <label className="form-label col-sm-6 col-lg-3 mb-0">
        Rule ID
        <input
          name="rule"
          defaultValue={filters.rule ?? ""}
          maxLength={63}
          className="form-control mt-1"
        />
      </label>
      <div className="col-12 btn-list">
        <button type="submit" className="btn">
          Apply filters
        </button>
        {active && (
          <Link to="/exceptions" className="btn btn-ghost-secondary">
            Clear
          </Link>
        )}
      </div>
    </form>
  );
}

const columns: readonly Column<PolicyException>[] = [
  {
    key: "exception",
    header: "Exception",
    rowHeader: true,
    render: (exception) => (
      <Link to={`/exceptions/${String(exception.id)}`} className="fw-medium">
        {`Exception ${String(exception.id)}`}
      </Link>
    ),
  },
  {
    key: "project",
    header: "Project",
    render: (exception) => exception.project.path,
  },
  {
    key: "rule",
    header: "Policy and rule",
    render: (exception) => `${exception.policy_name} / ${exception.rule_id}`,
  },
  {
    key: "state",
    header: "State",
    render: (exception) => (
      <>
        <Badge tone={stateTone[exception.state]}>{exceptionStateLabels[exception.state]}</Badge>
        {exception.in_force && <span className="ms-1 text-secondary">(in force)</span>}
      </>
    ),
  },
  { key: "by", header: "Requested by", render: requesterName, priority: 2 },
  {
    key: "expires",
    header: "Expires",
    render: (exception) => <span className="text-nowrap">{formatTime(exception.expires_at)}</span>,
    priority: 2,
  },
];

function Listing({ filters }: { filters: ExceptionFilters }) {
  const key = writeExceptionFilters(filters).toString();
  const first = useLoad(`exceptions?${key}`, (signal) => listExceptions(filters, null, signal));
  const paged = usePaged<PolicyException, ExceptionPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listExceptions(filters, after),
  );

  if (first.error) {
    return (
      <div className="card-body">
        <ErrorMessage error={first.error} retry={first.retry} />
      </div>
    );
  }
  if (first.loading) {
    return (
      <div className="card-body">
        <Loading label="Loading exceptions…" />
      </div>
    );
  }
  if (paged.items.length === 0) {
    return (
      <EmptyState
        title={key ? "No exceptions match these filters." : "No exceptions have been requested."}
      />
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <>
      <ResponsiveTable
        caption="Policy exceptions"
        columns={columns}
        rows={paged.items}
        rowKey={(exception) => exception.id}
      />
      <div className="card-footer d-flex flex-wrap align-items-center gap-2">
        <p className="text-secondary mb-0 me-auto" aria-live="polite">
          {`Showing ${String(paged.items.length)} exceptions${more}.`}
        </p>
        {paged.next !== null && (
          <button
            type="button"
            onClick={() => void paged.loadMore()}
            disabled={paged.pending}
            className="btn"
          >
            {paged.pending ? "Loading…" : "Load more exceptions"}
          </button>
        )}
      </div>
      {paged.moreError && (
        <div className="card-body">
          <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
        </div>
      )}
    </>
  );
}

export function ExceptionsPage() {
  const [params] = useSearchParams();
  const filters = readExceptionFilters(params);

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Act"
        title="Exceptions"
        subtitle={`Time-limited waivers of one rule for one project. An approved exception records a known failure as excepted, never as passing, and never covers unknown or unsupported results. Someone other than the requester must approve it, and every step is audited. "Expired" includes any exception past its expiry, even before the next evaluation records it.`}
      />
      <div className="card">
        <Filters filters={filters} />
        <Listing filters={filters} />
      </div>
    </section>
  );
}
