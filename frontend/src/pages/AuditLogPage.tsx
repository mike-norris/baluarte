import type { FormEvent } from "react";
import { useSearchParams } from "react-router";

import {
  listAuditActions,
  listAuditEvents,
  type AuditEvent,
  type AuditEventPage,
  type AuditFilter,
} from "../api/audit";
import { useLoad } from "../api/useLoad";
import { usePaged } from "../api/usePaged";
import { ErrorMessage } from "../components/ErrorMessage";
import { formatTime } from "../time";
import { PageHeader } from "../ui/PageHeader";

const cell = "px-3 py-2 align-top";
const DATE = /^\d{4}-\d{2}-\d{2}$/;
// Keeps a summary to one readable line; the full details are one click away.
const SUMMARY_LENGTH = 120;

/** The next UTC day, so "to" includes the whole day chosen. */
function dayAfter(date: string): string {
  const next = new Date(`${date}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return next.toISOString();
}

/** The API filter for the page's search parameters; malformed dates are ignored. */
export function auditFilter(params: URLSearchParams): AuditFilter {
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  return {
    action: params.get("action") || undefined,
    actor: params.get("actor")?.trim() || undefined,
    since: DATE.test(from) ? `${from}T00:00:00Z` : undefined,
    until: DATE.test(to) ? dayAfter(to) : undefined,
  };
}

function summary(details: Record<string, unknown>): string {
  const text = Object.entries(details)
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
  if (!text) return "No details";
  return text.length > SUMMARY_LENGTH ? `${text.slice(0, SUMMARY_LENGTH - 1)}…` : text;
}

function EventRow({ event }: { event: AuditEvent }) {
  const target = event.resource_id
    ? `${event.resource_type} ${event.resource_id}`
    : event.resource_type;
  return (
    <tr>
      <td className={`${cell} whitespace-nowrap`}>{formatTime(event.occurred_at)}</td>
      <td className={`${cell} font-mono text-xs`}>{event.actor}</td>
      <th scope="row" className={`${cell} font-mono text-xs font-medium`}>
        {event.action}
      </th>
      <td className={cell}>{target}</td>
      <td className={cell}>
        <details>
          <summary>{summary(event.details)}</summary>
          <pre className="alert alert-secondary text-body mt-1">
            {JSON.stringify(event.details, null, 2)}
          </pre>
          <p className="mt-1 font-monospace small text-break text-secondary">
            {`SHA-256 ${event.details_digest}`}
          </p>
        </details>
      </td>
      <td className={cell}>
        {event.digest_valid ? (
          <span className="text-success-emphasis">Verifies</span>
        ) : (
          <span className="fw-medium text-danger-emphasis">
            Does not match: the details changed after they were recorded
          </span>
        )}
      </td>
    </tr>
  );
}

function Filters({
  params,
  onApply,
}: {
  params: URLSearchParams;
  onApply: (next: URLSearchParams) => void;
}) {
  const actions = useLoad("audit-actions", listAuditActions);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    for (const name of ["action", "actor", "from", "to"]) {
      const value = form.get(name);
      if (typeof value === "string" && value.trim()) next.set(name, value.trim());
    }
    onApply(next);
  }

  return (
    <form
      onSubmit={submit}
      aria-label="Filter the audit log"
      className="grid gap-3 rounded border p-3 sm:grid-cols-2 lg:grid-cols-5"
    >
      <label className="fw-medium">
        Action
        <select
          name="action"
          defaultValue={params.get("action") ?? ""}
          className="form-select mt-1"
        >
          <option value="">Any</option>
          {(actions.data ?? []).map((action) => (
            <option key={action} value={action}>
              {action}
            </option>
          ))}
        </select>
      </label>
      <label className="fw-medium">
        Actor
        <input
          name="actor"
          defaultValue={params.get("actor") ?? ""}
          placeholder="user:12"
          maxLength={300}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        From (UTC)
        <input
          name="from"
          type="date"
          defaultValue={params.get("from") ?? ""}
          className="form-control mt-1"
        />
      </label>
      <label className="fw-medium">
        To (UTC, inclusive)
        <input
          name="to"
          type="date"
          defaultValue={params.get("to") ?? ""}
          className="form-control mt-1"
        />
      </label>
      <div className="d-flex align-items-end gap-2">
        <button type="submit" className="btn btn-primary">
          Apply
        </button>
        <button
          type="button"
          onClick={() => {
            onApply(new URLSearchParams());
          }}
          className="btn"
        >
          Clear
        </button>
      </div>
    </form>
  );
}

/** Who did what in Baluarte, newest first, with each event's digest check. */
export function AuditLogPage() {
  const [params, setParams] = useSearchParams();
  const filter = auditFilter(params);
  const key = `audit-events:${JSON.stringify(filter)}`;
  const first = useLoad(key, (signal) => listAuditEvents(filter, null, signal));
  const paged = usePaged<AuditEvent, AuditEventPage>(
    key,
    first,
    (page) => page.next_before_id,
    (next) => listAuditEvents(filter, next),
  );

  let body;
  if (first.error) body = <ErrorMessage error={first.error} retry={first.retry} />;
  else if (!first.data) {
    body = (
      <p role="status" className="text-secondary">
        Loading the audit log…
      </p>
    );
  } else if (paged.items.length === 0) {
    body = <p className="text-secondary">No events match these filters.</p>;
  } else {
    const invalid = paged.items.filter((event) => !event.digest_valid).length;
    body = (
      <div>
        {invalid > 0 && (
          <p role="alert" className="alert alert-danger text-body">
            {`${String(invalid)} ${invalid === 1 ? "event's details no longer match its" : "events' details no longer match their"} recorded digest. Compare with the baluarte.audit log stream.`}
          </p>
        )}
        <div className="card table-responsive">
          <table className="table table-vcenter card-table mb-0">
            <caption className="visually-hidden">Audit events, newest first</caption>
            <thead>
              <tr>
                {["Time", "Actor", "Action", "Target", "Details", "Digest"].map((header) => (
                  <th key={header} scope="col" className={`${cell} font-medium`}>
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paged.items.map((event) => (
                <EventRow key={event.id} event={event} />
              ))}
            </tbody>
          </table>
        </div>
        {paged.moreError && <p className="text-danger-emphasis">{paged.moreError.message}</p>}
        {paged.next !== null && (
          <button
            type="button"
            disabled={paged.pending}
            onClick={() => void paged.loadMore()}
            className="btn"
          >
            {paged.pending ? "Loading…" : "Load older events"}
          </button>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="page-title">
      <div>
        <PageHeader
          pretitle="Evidence"
          title="Audit log"
          subtitle={
            <>
              Every sign-in, setting change, baseline change, exception decision and export, with
              who did it. Times are UTC. Each event&apos;s details carry a SHA-256 digest, checked
              again here.
            </>
          }
        />
      </div>
      <p className="alert alert-info text-body">
        This view reads Baluarte&apos;s database, which its database administrators can alter. Each
        event is also written to the <code className="font-monospace">baluarte.audit</code> log
        stream when it is committed; that stream, shipped to a store they cannot change, is the
        independent copy to compare with.
      </p>
      <Filters
        key={params.toString()}
        params={params}
        onApply={(next) => {
          setParams(next);
        }}
      />
      {body}
    </section>
  );
}
