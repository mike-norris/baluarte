import { useId, useState } from "react";

import { ApiError } from "../api/client";
import { personId } from "../api/controls";
import { decideBatch, listExclusions, targetText, type Exclusion } from "../api/scope";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { changePerson } from "../controls/format";
import { canWritePolicies } from "../policies/format";
import { formatTime } from "../time";

const MAX_NOTE = 2000;

/** Exclusions proposed together, decided together. */
interface Batch {
  id: string;
  items: Exclusion[];
}

function batches(items: Exclusion[]): Batch[] {
  const byId = new Map<string, Exclusion[]>();
  for (const item of items) byId.set(item.batch_id, [...(byId.get(item.batch_id) ?? []), item]);
  return [...byId].map(([id, rows]) => ({ id, items: rows }));
}

function batchText(batch: Batch): string {
  const n = batch.items.length;
  return `Exclude from monitoring: ${String(n)} ${n === 1 ? "target" : "targets"}`;
}

function PendingBatch({
  batch,
  user,
  onDecided,
}: {
  batch: Batch;
  user: CurrentUser | null;
  onDecided: (message: string) => void;
}) {
  const { refresh } = useAuth();
  const id = useId();
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const first = batch.items[0];
  const canDecide = canWritePolicies(user);
  const own =
    user !== null && first !== undefined && personId(first.proposed_by) === user.user_id;
  const summary = batchText(batch);

  async function decide(decision: "approve" | "reject") {
    if (!user) return;
    if (decision === "reject" && !note.trim()) {
      setFailure("Explain why in the note before rejecting.");
      return;
    }
    setSending(decision);
    setFailure(null);
    try {
      await decideBatch(
        batch.id,
        decision,
        decision === "reject" ? note.trim() : note.trim() || null,
        user.csrf_token,
      );
      onDecided(
        decision === "approve"
          ? `Approved: ${summary}. They are out of monitoring now.`
          : `Rejected: ${summary}. Monitoring did not change.`,
      );
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(null);
    }
  }

  if (!first) return null;
  return (
    <li className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <div>
        <h3 className="font-medium">{summary}</h3>
        <p className="text-sm text-slate-600">
          {`Proposed by ${changePerson(first.proposed_by)} on ${formatTime(first.proposed_at)}.`}
        </p>
      </div>
      <ul aria-label="Targets" className="max-h-40 list-disc overflow-y-auto pl-5 text-sm">
        {batch.items.map((item) => (
          <li key={item.id}>{targetText(item)}</li>
        ))}
      </ul>
      <dl className="space-y-1 text-sm">
        <div>
          <dt className="font-medium">Justification</dt>
          <dd className="whitespace-pre-wrap">{first.justification}</dd>
        </div>
      </dl>
      {failure && (
        <p role="alert" className="text-sm text-red-900">
          {failure}
        </p>
      )}
      {canDecide &&
        (own ? (
          <p className="text-sm text-slate-700">
            You proposed these exclusions, so someone else must approve or reject them.
          </p>
        ) : (
          <div>
            <label htmlFor={`${id}-note`} className="text-sm font-medium">
              Note (required to reject)
            </label>
            <textarea
              id={`${id}-note`}
              value={note}
              maxLength={MAX_NOTE}
              rows={2}
              onChange={(event) => {
                setNote(event.target.value);
              }}
              className={fieldClass}
            />
          </div>
        ))}
      {canDecide && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={own || sending !== null}
            onClick={() => void decide("approve")}
            aria-describedby={own ? `${id}-own` : undefined}
            className={buttonClass}
          >
            {sending === "approve" ? "Approving…" : "Approve"}
          </button>
          <button
            type="button"
            disabled={own || sending !== null}
            onClick={() => void decide("reject")}
            aria-describedby={own ? `${id}-own` : undefined}
            className={secondaryClass}
          >
            {sending === "reject" ? "Rejecting…" : "Reject"}
          </button>
          {own && (
            <span id={`${id}-own`} className="sr-only">
              You cannot decide your own change.
            </span>
          )}
        </div>
      )}
    </li>
  );
}

/** Proposed exclusions from monitoring, decided a batch at a time like baseline changes. */
export function ScopeReview({
  reload,
  onDecided,
}: {
  reload: number;
  onDecided: (message: string) => void;
}) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const pending = useLoad(`scope:pending#${String(reload)}`, (signal) =>
    listExclusions("proposed", signal),
  );
  let body;
  if (pending.error) body = <ErrorMessage error={pending.error} retry={pending.retry} />;
  else if (!pending.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading proposed exclusions…
      </p>
    );
  } else if (pending.data.items.length === 0) {
    body = <p className="text-sm text-slate-600">No exclusion from monitoring is waiting.</p>;
  } else {
    body = (
      <ul className="space-y-3" aria-label="Exclusions from monitoring waiting for review">
        {batches(pending.data.items).map((batch) => (
          <PendingBatch key={batch.id} batch={batch} user={user} onDecided={onDecided} />
        ))}
      </ul>
    );
  }
  return (
    <section aria-labelledby="scope-pending-title" className="space-y-3">
      <h2 id="scope-pending-title" className="text-lg font-semibold">
        Exclusions from monitoring waiting for review
      </h2>
      {body}
    </section>
  );
}
