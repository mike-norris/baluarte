import { useId, useState } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import {
  controlPath,
  decideMappingChange,
  listMappingChanges,
  type MappingChange,
} from "../api/controls";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { canWritePolicies } from "../policies/format";
import { formatTime } from "../time";
import { changePerson, isOwnChange, mappingChangeText } from "./format";

const MAX_NOTE = 2000;

function PendingMappingChange({
  change,
  user,
  onDecided,
}: {
  change: MappingChange;
  user: CurrentUser | null;
  onDecided: (message: string) => void;
}) {
  const { refresh } = useAuth();
  const id = useId();
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const canDecide = canWritePolicies(user);
  const own = isOwnChange(change, user?.user_id);
  const summary = mappingChangeText(change);

  async function decide(decision: "approve" | "reject") {
    if (!user) return;
    if (decision === "reject" && !note.trim()) {
      setFailure("Explain why in the note before rejecting.");
      return;
    }
    setSending(decision);
    setFailure(null);
    try {
      await decideMappingChange(
        change.id,
        decision,
        decision === "reject" ? note.trim() : note.trim() || null,
        user.csrf_token,
      );
      onDecided(
        decision === "approve"
          ? `Approved: ${summary}. It is in force now.`
          : `Rejected: ${summary}. The mapping did not change.`,
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

  return (
    <li className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <div>
        <h3 className="font-medium">{summary}</h3>
        <p className="text-sm text-slate-600">
          {`Proposed by ${changePerson(change.proposed_by)} on ${formatTime(change.proposed_at)}.`}{" "}
          <Link to={controlPath(change.framework, change.control_id)} className={linkClass}>
            {`View ${change.control_id}`}
          </Link>
        </p>
      </div>
      <dl className="space-y-1 text-sm">
        <div>
          <dt className="font-medium">Justification</dt>
          <dd className="whitespace-pre-wrap">{change.justification}</dd>
        </div>
        {change.evidence_ref && (
          <div>
            <dt className="font-medium">Where the evidence is</dt>
            <dd>{change.evidence_ref}</dd>
          </div>
        )}
        {change.review_by && (
          <div>
            <dt className="font-medium">Review by</dt>
            <dd>{change.review_by}</dd>
          </div>
        )}
      </dl>
      {failure && (
        <p role="alert" className="text-sm text-red-900">
          {failure}
        </p>
      )}
      {canDecide &&
        (own ? (
          <p className="text-sm text-slate-700">
            You proposed this change, so someone else must approve or reject it.
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

/** Proposed changes to the control mapping, decided like baseline changes. */
export function MappingReview({
  reload,
  onDecided,
}: {
  reload: number;
  onDecided: (message: string) => void;
}) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const pending = useLoad(`control-mapping:pending#${String(reload)}`, (signal) =>
    listMappingChanges("proposed", signal),
  );
  let body;
  if (pending.error) body = <ErrorMessage error={pending.error} retry={pending.retry} />;
  else if (!pending.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading mapping changes…
      </p>
    );
  } else if (pending.data.items.length === 0) {
    body = <p className="text-sm text-slate-600">No control mapping change is waiting.</p>;
  } else {
    body = (
      <ul className="space-y-3" aria-label="Control mapping changes waiting for review">
        {pending.data.items.map((change) => (
          <PendingMappingChange
            key={change.id}
            change={change}
            user={user}
            onDecided={onDecided}
          />
        ))}
      </ul>
    );
  }
  return (
    <section aria-labelledby="mapping-pending-title" className="space-y-3">
      <h2 id="mapping-pending-title" className="text-lg font-semibold">
        Control mapping changes waiting for review
      </h2>
      {body}
    </section>
  );
}
