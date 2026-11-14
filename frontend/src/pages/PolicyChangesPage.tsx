import { useEffect, useId, useRef, useState } from "react";
import { Link, useLocation } from "react-router";

import { ApiError } from "../api/client";
import {
  approveChange,
  getPolicyChange,
  listPolicyChanges,
  rejectChange,
  type PolicyChange,
  type PolicyChangePage,
} from "../api/policies";
import { useLoad } from "../api/useLoad";
import { usePaged } from "../api/usePaged";
import { useAuth } from "../auth/context";
import { leavesOwnToOthers, type CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { MappingReview } from "../controls/MappingReview";
import { ScopeReview } from "../inventory/ScopeReview";
import { TaxonomyReview } from "../labels/TaxonomyReview";
import { lineDiff } from "../policies/diff";
import { baselineHref, canWritePolicies, changeText, personText } from "../policies/format";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { formatTime } from "../time";
import type { SavedState } from "./BaselineEditorPage";

const cell = "align-top";
const MAX_NOTE = 2000;

const stateLabels: Record<PolicyChange["state"], string> = {
  proposed: "Waiting for review",
  approved: "Approved",
  rejected: "Rejected",
};

/** How a decided change was reviewed, in words an auditor can quote. */
function decisionText(change: PolicyChange): string {
  if (change.migrated) return "In force before review was recorded";
  if (change.self_approved) return `Not reviewed: review was off, so it applied at once`;
  const by = personText(change.decided_by);
  return change.state === "rejected" ? `Rejected by ${by}` : `Approved by ${by}`;
}

function Diff({ id }: { id: number }) {
  const detail = useLoad(`policy-change:${String(id)}`, (signal) => getPolicyChange(id, signal));
  if (detail.error) return <ErrorMessage error={detail.error} retry={detail.retry} />;
  if (!detail.data) {
    return (
      <p role="status" className="text-secondary">
        Loading the proposed version…
      </p>
    );
  }
  const { source_yaml: proposed, base_source_yaml: base, base_version: baseVersion } = detail.data;
  if (proposed === null) return null;
  const lines = lineDiff(base ?? "", proposed);
  const caption =
    baseVersion === null
      ? "The proposed baseline (new)"
      : `Changes from v${String(baseVersion)}, the version in force`;
  return (
    <figure>
      <figcaption className="fw-medium">{caption}</figcaption>
      <pre aria-label={caption} className="small mb-0 overflow-auto" style={{ maxHeight: "24rem" }}>
        {lines.map((line, index) => (
          <span
            key={index}
            className={`d-block ${
              line.kind === "added"
                ? "bg-success-subtle text-success-emphasis"
                : line.kind === "removed"
                  ? "bg-danger-subtle text-danger-emphasis"
                  : ""
            }`}
          >
            {`${line.kind === "added" ? "+ " : line.kind === "removed" ? "- " : "  "}${line.text}`}
          </span>
        ))}
      </pre>
      <p className="small text-secondary">
        Lines starting with + are added, and lines starting with - are removed.
      </p>
    </figure>
  );
}

function PendingChange({
  change,
  user,
  onDecided,
}: {
  change: PolicyChange;
  user: CurrentUser | null;
  onDecided: (message: string) => void;
}) {
  const { refresh } = useAuth();
  const id = useId();
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const canDecide = canWritePolicies(user);
  const own = leavesOwnToOthers(user, change.proposed_by.user_id);
  const summary = changeText(change);

  async function decide(decision: "approve" | "reject") {
    if (!user) return;
    if (decision === "reject" && !note.trim()) {
      setFailure("Explain why in the note before rejecting.");
      return;
    }
    setSending(decision);
    setFailure(null);
    try {
      if (decision === "approve") {
        await approveChange(change.id, note.trim() || null, user.csrf_token);
        onDecided(`Approved: ${summary}. It is in force now.`);
      } else {
        await rejectChange(change.id, note.trim(), user.csrf_token);
        onDecided(`Rejected: ${summary}. Nothing changed.`);
      }
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
          {`Proposed by ${personText(change.proposed_by)} on ${formatTime(change.proposed_at)}.`}
        </p>
      </div>
      {change.kind === "version" ? (
        <Diff id={change.id} />
      ) : (
        <p>
          {change.kind === "assign"
            ? "Projects there will be checked against this version once it is approved."
            : "Projects there fall back to whatever is applied above, or to nothing, once it is approved."}{" "}
          {change.version !== null && (
            <Link to={baselineHref(change.policy_name, change.version)}>
              {`View ${change.policy_name} v${String(change.version)}`}
            </Link>
          )}
        </p>
      )}
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

function History({ reload }: { reload: number }) {
  const key = `policy-changes:all#${String(reload)}`;
  const first = useLoad(key, (signal) => listPolicyChanges({}, signal));
  const paged = usePaged<PolicyChange, PolicyChangePage>(
    key,
    first,
    (page) => page.next_before_id,
    (next) => listPolicyChanges({ beforeId: next }),
  );
  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (!first.data) {
    return (
      <p role="status" className="text-secondary">
        Loading history…
      </p>
    );
  }
  const decided = paged.items.filter((change) => change.state !== "proposed");
  if (decided.length === 0 && paged.next === null) {
    return <p className="text-secondary">No baseline changes are recorded yet.</p>;
  }
  return (
    <div>
      <div className="card table-responsive">
        <table className="table table-vcenter card-table mb-0">
          <caption className="visually-hidden">Decided baseline changes</caption>
          <thead>
            <tr>
              {["Change", "Outcome", "Proposed", "Review"].map((header) => (
                <th key={header} scope="col" className={`${cell} font-medium`}>
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {decided.map((change) => (
              <tr key={change.id}>
                <th scope="row" className={`${cell} font-medium`}>
                  {changeText(change)}
                </th>
                <td className={cell}>{stateLabels[change.state]}</td>
                <td className={cell}>
                  {personText(change.proposed_by)}
                  <span className="d-block small text-nowrap text-secondary">
                    {formatTime(change.proposed_at)}
                  </span>
                </td>
                <td className={cell}>
                  {decisionText(change)}
                  {change.decision_note && (
                    <span className="d-block small text-secondary">{`“${change.decision_note}”`}</span>
                  )}
                </td>
              </tr>
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
          {paged.pending ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}

/** Proposed baseline changes to approve or reject, and the record of decided ones. */
export function PolicyChangesPage() {
  const location = useLocation();
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const [reload, setReload] = useState(0);
  const [message, setMessage] = useState<string | null>(
    (location.state as SavedState | null)?.message ?? null,
  );
  const status = useRef<HTMLParagraphElement>(null);
  const pending = useLoad(`policy-changes:pending#${String(reload)}`, (signal) =>
    listPolicyChanges({ state: "proposed" }, signal),
  );

  useEffect(() => {
    if (message) status.current?.focus();
  }, [message]);

  function decided(text: string) {
    setMessage(text);
    setReload((value) => value + 1);
  }

  let waiting;
  if (pending.error) waiting = <ErrorMessage error={pending.error} retry={pending.retry} />;
  else if (!pending.data) {
    waiting = (
      <p role="status" className="text-secondary">
        Loading changes…
      </p>
    );
  } else if (pending.data.items.length === 0) {
    waiting = <p className="text-secondary">Nothing is waiting for review.</p>;
  } else {
    waiting = (
      <ul aria-label="Changes waiting for review">
        {pending.data.items.map((change) => (
          <PendingChange key={change.id} change={change} user={user} onDecided={decided} />
        ))}
      </ul>
    );
  }

  return (
    <section aria-labelledby="page-title">
      <GovernanceHeader intro="Every new baseline version, every change to where baselines apply, and every change to the control mapping is recorded here with who proposed it and who approved it. When review is required, a change takes effect only after someone other than its proposer approves it." />
      {message && (
        <p ref={status} tabIndex={-1} role="status" className="alert alert-success text-body">
          {message}
        </p>
      )}
      <section aria-labelledby="pending-title">
        <h2 id="pending-title" className="card-title">
          Waiting for review
        </h2>
        {waiting}
      </section>
      <MappingReview reload={reload} onDecided={decided} />
      <ScopeReview reload={reload} onDecided={decided} />
      <TaxonomyReview reload={reload} onDecided={decided} />
      <section aria-labelledby="history-title">
        <h2 id="history-title" className="card-title">
          Decided changes
        </h2>
        <History reload={reload} />
      </section>
    </section>
  );
}
