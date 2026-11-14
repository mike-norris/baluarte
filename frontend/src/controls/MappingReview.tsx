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
  const own = isOwnChange(change, user?.user_id) && user?.development_mode !== true;
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
    <li className="card card-body mb-3">
      <div>
        <h3 className="fw-medium">{summary}</h3>
        <p className="text-secondary">
          {`Proposed by ${changePerson(change.proposed_by)} on ${formatTime(change.proposed_at)}.`}{" "}
          <Link to={controlPath(change.framework, change.control_id)}>
            {`View ${change.control_id}`}
          </Link>
        </p>
      </div>
      <dl >
        <div>
          <dt className="fw-medium">Justification</dt>
          <dd className="text-prewrap">{change.justification}</dd>
        </div>
        {change.evidence_ref && (
          <div>
            <dt className="fw-medium">Where the evidence is</dt>
            <dd>{change.evidence_ref}</dd>
          </div>
        )}
        {change.review_by && (
          <div>
            <dt className="fw-medium">Review by</dt>
            <dd>{change.review_by}</dd>
          </div>
        )}
      </dl>
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
      {canDecide &&
        (own ? (
          <p className="text-secondary">
            You proposed this change, so someone else must approve or reject it.
          </p>
        ) : (
          <div>
            <label htmlFor={`${id}-note`} className="fw-medium">
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
              className="form-control mt-1"
            />
          </div>
        ))}
      {canDecide && (
        <div className="d-flex flex-wrap gap-2">
          <button
            type="button"
            disabled={own || sending !== null}
            onClick={() => void decide("approve")}
            aria-describedby={own ? `${id}-own` : undefined}
            className="btn btn-primary"
          >
            {sending === "approve" ? "Approving…" : "Approve"}
          </button>
          <button
            type="button"
            disabled={own || sending !== null}
            onClick={() => void decide("reject")}
            aria-describedby={own ? `${id}-own` : undefined}
            className="btn"
          >
            {sending === "reject" ? "Rejecting…" : "Reject"}
          </button>
          {own && (
            <span id={`${id}-own`} className="visually-hidden">
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
      <p role="status" className="text-secondary">
        Loading mapping changes…
      </p>
    );
  } else if (pending.data.items.length === 0) {
    body = <p className="text-secondary">No control mapping change is waiting.</p>;
  } else {
    body = (
      <ul  aria-label="Control mapping changes waiting for review">
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
    <section aria-labelledby="mapping-pending-title" >
      <h2 id="mapping-pending-title" className="card-title">
        Control mapping changes waiting for review
      </h2>
      {body}
    </section>
  );
}
