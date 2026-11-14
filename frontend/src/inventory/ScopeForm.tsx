import { useId, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import {
  endExclusions,
  justificationProblem,
  MAX_JUSTIFICATION,
  proposeExclusions,
  type ExclusionBatch,
  type ExclusionTarget,
} from "../api/scope";
import { useAuth } from "../auth/context";

/** What the form changes: targets to exclude, or exclusions to end. */
export type ScopeIntent =
  | {
      mode: "exclude";
      targets: ExclusionTarget[];
      personalNamespaces?: boolean;
      /** The selection in words, one line each. */
      labels: string[];
    }
  | { mode: "include"; ids: number[]; labels: string[] };

function count(n: number, one: string, many: string): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

/** What happened, in a sentence: applied at once, or waiting for someone else. */
export function excludedText(batch: ExclusionBatch, fallback: number): string {
  const items = Array.isArray(batch.items) ? batch.items : [];
  const total = items.length > 0 ? items.length : fallback;
  const what = count(total, "exclusion", "exclusions");
  if (items.some((item) => item.state === "proposed")) {
    return `Proposed for review: ${what}. Monitoring changes once someone else approves.`;
  }
  return `Excluded from monitoring: ${what}, in force now.`;
}

/**
 * Exclude the selection from monitoring, or include it again, with the justification the API
 * requires. Excluding follows two-person review when it is on; including again applies at once.
 */
export function ScopeForm({
  intent,
  onDone,
  onCancel,
}: {
  intent: ScopeIntent;
  onDone: (message: string) => void;
  onCancel: () => void;
}) {
  const { session, refresh } = useAuth();
  const id = useId();
  const [justification, setJustification] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const user = session.status === "signed_in" ? session.user : null;
  const exclude = intent.mode === "exclude";
  const title = exclude ? "Exclude from monitoring" : "Include in monitoring again";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user) return;
    const invalid = justificationProblem(justification);
    if (invalid) {
      setProblem(`Justification: ${invalid}`);
      return;
    }
    setSending(true);
    setProblem(null);
    try {
      if (intent.mode === "exclude") {
        const batch = await proposeExclusions(
          {
            targets: intent.targets,
            personal_namespaces: intent.personalNamespaces ?? false,
            justification: justification.trim(),
          },
          user.csrf_token,
        );
        onDone(excludedText(batch, intent.labels.length));
      } else {
        await endExclusions(intent.ids, justification.trim(), user.csrf_token);
        onDone(
          `Included in monitoring again: ${count(intent.ids.length, "exclusion", "exclusions")} ended. The next evaluation checks them.`,
        );
      }
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setProblem(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      aria-label={title}
      onSubmit={(event) => void submit(event)}
      className="rounded border p-3"
    >
      <h2 className="card-title">{title}</h2>
      <p className="text-secondary">
        {exclude
          ? "Excluded projects stay in the inventory but are not evaluated, read in detail or counted. Their open findings are resolved. You can include them again at any time."
          : "Included projects are evaluated again from the next evaluation; findings reopen if the rules still fail."}
      </p>
      <ul aria-label="Selected" className="ps-3 overflow-auto" style={{ maxHeight: "10rem" }}>
        {intent.labels.map((label) => (
          <li key={label}>{label}</li>
        ))}
      </ul>
      <div>
        <label htmlFor={`${id}-why`} className="fw-medium">
          Justification
        </label>
        <textarea
          id={`${id}-why`}
          value={justification}
          maxLength={MAX_JUSTIFICATION}
          rows={3}
          aria-describedby={`${id}-help`}
          onChange={(event) => {
            setJustification(event.target.value);
          }}
          className="form-control mt-1"
        />
        <p id={`${id}-help`} className="mt-1 small text-secondary">
          {exclude
            ? "Why these are out of scope, for example research or personal work. 20 to 2000 characters; kept as evidence."
            : "Why they are monitored again. 20 to 2000 characters; kept as evidence."}
        </p>
      </div>
      {problem && (
        <p role="alert" className="text-danger-emphasis">
          {problem}
        </p>
      )}
      <div className="d-flex flex-wrap gap-2">
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Sending…" : exclude ? "Exclude" : "Include again"}
        </button>
        <button type="button" onClick={onCancel} className="btn">
          Cancel
        </button>
      </div>
    </form>
  );
}
