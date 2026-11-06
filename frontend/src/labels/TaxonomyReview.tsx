import { useId, useState } from "react";

import { ApiError } from "../api/client";
import { decideTaxonomy, getTaxonomy, MAX_NOTE, type TaxonomyVersion } from "../api/labels";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { lineDiff } from "../policies/diff";
import { canWritePolicies, personText } from "../policies/format";
import { formatTime } from "../time";

function versionText(version: TaxonomyVersion): string {
  const count = version.spec?.labels.length;
  const labels = count === undefined ? "" : `, ${String(count)} ${count === 1 ? "label" : "labels"}`;
  return `Label taxonomy ${version.name} v${String(version.version)}${labels}`;
}

function YamlDiff({ base, proposed }: { base: string | undefined; proposed: string | undefined }) {
  if (proposed === undefined) return null;
  const caption = base === undefined ? "The proposed taxonomy" : "Changes from the taxonomy in force";
  return (
    <figure className="space-y-1">
      <figcaption className="text-sm font-medium">{caption}</figcaption>
      <pre
        aria-label={caption}
        className="max-h-80 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100"
      >
        {lineDiff(base ?? "", proposed).map((line, index) => (
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
    </figure>
  );
}

function PendingVersion({
  version,
  current,
  user,
  onDecided,
}: {
  version: TaxonomyVersion;
  current: TaxonomyVersion | null;
  user: CurrentUser | null;
  onDecided: (message: string) => void;
}) {
  const { refresh } = useAuth();
  const id = useId();
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const canDecide = canWritePolicies(user);
  const own = user !== null && version.proposed_by.user_id === user.user_id;
  const summary = versionText(version);

  async function decide(decision: "approve" | "reject") {
    if (!user) return;
    if (decision === "reject" && !note.trim()) {
      setFailure("Explain why in the note before rejecting.");
      return;
    }
    setSending(decision);
    setFailure(null);
    try {
      await decideTaxonomy(version.id, decision, note, user.csrf_token);
      onDecided(
        decision === "approve"
          ? `Approved: ${summary}. It is in force now; drift is measured against it.`
          : `Rejected: ${summary}. The taxonomy in force did not change.`,
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
          {`Proposed by ${personText(version.proposed_by)} on ${formatTime(version.proposed_at)}.`}
        </p>
      </div>
      {version.justification && (
        <dl className="text-sm">
          <dt className="font-medium">Justification</dt>
          <dd className="whitespace-pre-wrap">{version.justification}</dd>
        </dl>
      )}
      <YamlDiff base={current?.source_yaml} proposed={version.source_yaml} />
      {failure && (
        <p role="alert" className="text-sm text-red-900">
          {failure}
        </p>
      )}
      {canDecide &&
        (own ? (
          <p className="text-sm text-slate-700">
            You proposed this taxonomy, so someone else must approve or reject it.
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
      {canDecide && !own && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={sending !== null}
            onClick={() => void decide("approve")}
            className={buttonClass}
          >
            {sending === "approve" ? "Approving…" : "Approve"}
          </button>
          <button
            type="button"
            disabled={sending !== null}
            onClick={() => void decide("reject")}
            className={secondaryClass}
          >
            {sending === "reject" ? "Rejecting…" : "Reject"}
          </button>
        </div>
      )}
    </li>
  );
}

/** Proposed label taxonomies, decided like baseline changes: never by their proposer. */
export function TaxonomyReview({
  reload,
  onDecided,
}: {
  reload: number;
  onDecided: (message: string) => void;
}) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const taxonomy = useLoad(`labels:taxonomy#${String(reload)}`, (signal) =>
    getTaxonomy(null, signal),
  );
  let body;
  if (taxonomy.error) body = <ErrorMessage error={taxonomy.error} retry={taxonomy.retry} />;
  else if (!taxonomy.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading proposed label taxonomies…
      </p>
    );
  } else {
    const { current, versions } = taxonomy.data;
    const pending = versions.filter((version) => version.state === "proposed");
    body =
      pending.length === 0 ? (
        <p className="text-sm text-slate-600">No label taxonomy is waiting.</p>
      ) : (
        <ul className="space-y-3" aria-label="Label taxonomies waiting for review">
          {pending.map((version) => (
            <PendingVersion
              key={version.id}
              version={version}
              current={current}
              user={user}
              onDecided={onDecided}
            />
          ))}
        </ul>
      );
  }
  return (
    <section aria-labelledby="taxonomy-pending-title" className="space-y-3">
      <h2 id="taxonomy-pending-title" className="text-lg font-semibold">
        Label taxonomies waiting for review
      </h2>
      {body}
    </section>
  );
}
