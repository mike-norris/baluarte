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
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { lineDiff } from "../policies/diff";
import { baselineHref, canWritePolicies, changeText, personText } from "../policies/format";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { formatTime } from "../time";
import type { SavedState } from "./BaselineEditorPage";

const cell = "px-3 py-2";
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
      <p role="status" className="text-sm text-slate-600">
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
    <figure className="space-y-1">
      <figcaption className="text-sm font-medium">{caption}</figcaption>
      <pre
        aria-label={caption}
        className="max-h-96 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100"
      >
        {lines.map((line, index) => (
          <span
            key={index}
            className={`block ${
              line.kind === "added"
                ? "bg-emerald-900 text-emerald-100"
                : line.kind === "removed"
                  ? "bg-red-900 text-red-100"
                  : ""
            }`}
          >
            {`${line.kind === "added" ? "+ " : line.kind === "removed" ? "- " : "  "}${line.text}`}
          </span>
        ))}
      </pre>
      <p className="text-xs text-slate-600">
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
  const own = user !== null && change.proposed_by.user_id === user.user_id;
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
    <li className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <div>
        <h3 className="font-medium">{summary}</h3>
        <p className="text-sm text-slate-600">
          {`Proposed by ${personText(change.proposed_by)} on ${formatTime(change.proposed_at)}.`}
        </p>
      </div>
      {change.kind === "version" ? (
        <Diff id={change.id} />
      ) : (
        <p className="text-sm">
          {change.kind === "assign"
            ? "Projects there will be checked against this version once it is approved."
            : "Projects there fall back to whatever is applied above, or to nothing, once it is approved."}{" "}
          {change.version !== null && (
            <Link to={baselineHref(change.policy_name, change.version)} className={linkClass}>
              {`View ${change.policy_name} v${String(change.version)}`}
            </Link>
          )}
        </p>
      )}
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
      <p role="status" className="text-slate-600">
        Loading history…
      </p>
    );
  }
  const decided = paged.items.filter((change) => change.state !== "proposed");
  if (decided.length === 0 && paged.next === null) {
    return <p className="text-sm text-slate-600">No baseline changes are recorded yet.</p>;
  }
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Decided baseline changes</caption>
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              {["Change", "Outcome", "Proposed", "Review"].map((header) => (
                <th key={header} scope="col" className={`${cell} font-medium`}>
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {decided.map((change) => (
              <tr key={change.id}>
                <th scope="row" className={`${cell} font-medium`}>
                  {changeText(change)}
                </th>
                <td className={cell}>{stateLabels[change.state]}</td>
                <td className={cell}>
                  {personText(change.proposed_by)}
                  <span className="block text-xs whitespace-nowrap text-slate-600">
                    {formatTime(change.proposed_at)}
                  </span>
                </td>
                <td className={cell}>
                  {decisionText(change)}
                  {change.decision_note && (
                    <span className="block text-xs text-slate-600">{`“${change.decision_note}”`}</span>
                  )}
                </td>
              </tr>
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
      <p role="status" className="text-slate-600">
        Loading changes…
      </p>
    );
  } else if (pending.data.items.length === 0) {
    waiting = <p className="text-sm text-slate-600">Nothing is waiting for review.</p>;
  } else {
    waiting = (
      <ul className="space-y-3" aria-label="Changes waiting for review">
        {pending.data.items.map((change) => (
          <PendingChange key={change.id} change={change} user={user} onDecided={decided} />
        ))}
      </ul>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <GovernanceHeader intro="Every new baseline version, and every change to where baselines apply, is recorded here with who proposed it and who approved it. When review is required, a change takes effect only after someone other than its proposer approves it." />
      {message && (
        <p
          ref={status}
          tabIndex={-1}
          role="status"
          className="rounded border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-950"
        >
          {message}
        </p>
      )}
      <section aria-labelledby="pending-title" className="space-y-3">
        <h2 id="pending-title" className="text-lg font-semibold">
          Waiting for review
        </h2>
        {waiting}
      </section>
      <section aria-labelledby="history-title" className="space-y-3">
        <h2 id="history-title" className="text-lg font-semibold">
          Decided changes
        </h2>
        <History reload={reload} />
      </section>
    </section>
  );
}
