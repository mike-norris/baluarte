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
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { formatTime } from "../time";

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
          <summary className="cursor-pointer">{summary(event.details)}</summary>
          <pre className="mt-1 max-w-xl overflow-x-auto rounded bg-slate-100 p-2 text-xs">
            {JSON.stringify(event.details, null, 2)}
          </pre>
          <p className="mt-1 font-mono text-xs break-all text-slate-600">
            {`SHA-256 ${event.details_digest}`}
          </p>
        </details>
      </td>
      <td className={cell}>
        {event.digest_valid ? (
          <span className="text-emerald-800">Verifies</span>
        ) : (
          <span className="font-medium text-red-800">
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
      className="grid gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-2 lg:grid-cols-5"
    >
      <label className="text-sm font-medium">
        Action
        <select name="action" defaultValue={params.get("action") ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {(actions.data ?? []).map((action) => (
            <option key={action} value={action}>
              {action}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm font-medium">
        Actor
        <input
          name="actor"
          defaultValue={params.get("actor") ?? ""}
          placeholder="user:12"
          maxLength={300}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        From (UTC)
        <input
          name="from"
          type="date"
          defaultValue={params.get("from") ?? ""}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        To (UTC, inclusive)
        <input name="to" type="date" defaultValue={params.get("to") ?? ""} className={fieldClass} />
      </label>
      <div className="flex items-end gap-2">
        <button type="submit" className={buttonClass}>
          Apply
        </button>
        <button
          type="button"
          onClick={() => {
            onApply(new URLSearchParams());
          }}
          className={secondaryClass}
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
      <p role="status" className="text-slate-600">
        Loading the audit log…
      </p>
    );
  } else if (paged.items.length === 0) {
    body = <p className="text-sm text-slate-600">No events match these filters.</p>;
  } else {
    const invalid = paged.items.filter((event) => !event.digest_valid).length;
    body = (
      <div className="space-y-2">
        {invalid > 0 && (
          <p
            role="alert"
            className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900"
          >
            {`${String(invalid)} ${invalid === 1 ? "event's details no longer match its" : "events' details no longer match their"} recorded digest. Compare with the baluarte.audit log stream.`}
          </p>
        )}
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Audit events, newest first</caption>
            <thead className="border-b border-slate-200 bg-slate-50">
              <tr>
                {["Time", "Actor", "Action", "Target", "Details", "Digest"].map((header) => (
                  <th key={header} scope="col" className={`${cell} font-medium`}>
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {paged.items.map((event) => (
                <EventRow key={event.id} event={event} />
              ))}
            </tbody>
          </table>
        </div>
        {paged.moreError && <p className="text-sm text-red-900">{paged.moreError.message}</p>}
        {paged.next !== null && (
          <button
            type="button"
            disabled={paged.pending}
            onClick={() => void paged.loadMore()}
            className={secondaryClass}
          >
            {paged.pending ? "Loading…" : "Load older events"}
          </button>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Audit log
        </h1>
        <p className="mt-2 max-w-3xl text-slate-600">
          Every sign-in, setting change, baseline change, exception decision and export, with who
          did it. Times are UTC. Each event&apos;s details carry a SHA-256 digest, checked again
          here.
        </p>
      </div>
      <p className="rounded border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
        This view reads Baluarte&apos;s database, which its database administrators can alter. Each
        event is also written to the <code className="font-mono">baluarte.audit</code> log stream
        when it is committed; that stream, shipped to a store they cannot change, is the independent
        copy to compare with.
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
