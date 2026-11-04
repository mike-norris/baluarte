import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import {
  proposeMappingChange,
  type FrameworkId,
  type MappingChange,
  type MappingChangeKind,
} from "../api/controls";
import { useAuth } from "../auth/context";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import {
  frameworkLabel,
  kindLabels,
  latestReviewBy,
  MAX_EVIDENCE_REF,
  MAX_JUSTIFICATION,
  MIN_JUSTIFICATION,
  needsReviewBy,
  ruleTypeLabel,
  validateChange,
} from "./format";

export interface ChangeIntent {
  kind: MappingChangeKind;
  /** ``map``: the rule types that may be added; ``unmap``: the one being removed. */
  ruleTypes?: readonly string[];
  /** ``withdraw``: the change being withdrawn, and what it did. */
  supersedes?: { id: number; text: string };
}

const help: Record<MappingChangeKind, string> = {
  map: "Results of this rule type will count toward the control.",
  unmap: "Results of this rule type will no longer count toward the control.",
  not_applicable:
    "The control is shown as not applicable until the review date, then as review overdue.",
  manual:
    "The control is shown as met outside GitLab until the review date, then as review overdue. Say where the evidence is kept.",
  withdraw: "The earlier change stops applying, and the mapping returns to what it was before it.",
};

/**
 * Propose one change to a control's mapping. Every change needs a justification, which the
 * evidence export carries; while review is required it waits for someone else to approve it.
 */
export function ChangeForm({
  framework,
  controlId,
  intent,
  onDone,
  onCancel,
}: {
  framework: FrameworkId;
  controlId: string;
  intent: ChangeIntent;
  onDone: (change: MappingChange) => void;
  onCancel: () => void;
}) {
  const { session, refresh } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const choices = intent.ruleTypes ?? [];
  const [ruleType, setRuleType] = useState(choices.length === 1 ? (choices[0] ?? "") : "");
  const [justification, setJustification] = useState("");
  const [evidenceRef, setEvidenceRef] = useState("");
  const [reviewBy, setReviewBy] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const datedKind = needsReviewBy(intent.kind);

  useEffect(() => {
    heading.current?.focus();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user) return;
    const checked = validateChange(framework, controlId, {
      kind: intent.kind,
      ruleType,
      justification,
      evidenceRef,
      reviewBy,
      supersedesId: intent.supersedes?.id ?? null,
    });
    if ("errors" in checked) {
      setErrors(checked.errors);
      return;
    }
    setErrors([]);
    setSending(true);
    try {
      onDone(await proposeMappingChange(checked.request, user.csrf_token));
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setErrors([apiError.message]);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  const control = `${frameworkLabel(framework)} ${controlId}`;
  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-labelledby={`${id}-title`}
      noValidate
      className="space-y-3 rounded border border-slate-300 bg-white p-4"
    >
      <h3 id={`${id}-title`} ref={heading} tabIndex={-1} className="font-semibold">
        {`${kindLabels[intent.kind]}: ${control}`}
      </h3>
      <p className="text-sm text-slate-700">{help[intent.kind]}</p>
      {intent.supersedes && (
        <p className="text-sm">{`Withdraws change ${String(intent.supersedes.id)}: ${intent.supersedes.text}.`}</p>
      )}
      {intent.kind === "unmap" && choices.length === 1 && (
        <p className="text-sm">{`Rule type: ${ruleTypeLabel(choices[0] ?? "")}`}</p>
      )}
      {intent.kind === "map" && (
        <label className="block text-sm font-medium">
          Rule type
          <select
            value={ruleType}
            onChange={(event) => {
              setRuleType(event.target.value);
            }}
            required
            className={fieldClass}
          >
            <option value="">Choose a rule type</option>
            {choices.map((type) => (
              <option key={type} value={type}>
                {`${ruleTypeLabel(type)} (${type})`}
              </option>
            ))}
          </select>
        </label>
      )}
      <div>
        <label htmlFor={`${id}-justification`} className="text-sm font-medium">
          Justification
        </label>
        <textarea
          id={`${id}-justification`}
          value={justification}
          minLength={MIN_JUSTIFICATION}
          maxLength={MAX_JUSTIFICATION}
          rows={4}
          required
          aria-describedby={`${id}-justification-help`}
          onChange={(event) => {
            setJustification(event.target.value);
          }}
          className={fieldClass}
        />
        <p id={`${id}-justification-help`} className="mt-1 text-xs text-slate-600">
          {`Why the organization makes this change, for reviewers and auditors. ${String(MIN_JUSTIFICATION)} to ${String(MAX_JUSTIFICATION)} characters.`}
        </p>
      </div>
      <div>
        <label htmlFor={`${id}-evidence`} className="text-sm font-medium">
          Where the evidence is (optional)
        </label>
        <input
          id={`${id}-evidence`}
          value={evidenceRef}
          maxLength={MAX_EVIDENCE_REF}
          placeholder="Policy document, ticket or system of record"
          onChange={(event) => {
            setEvidenceRef(event.target.value);
          }}
          className={fieldClass}
        />
      </div>
      {datedKind && (
        <div>
          <label htmlFor={`${id}-review`} className="text-sm font-medium">
            Review by
          </label>
          <input
            id={`${id}-review`}
            type="date"
            value={reviewBy}
            max={latestReviewBy(new Date())}
            required
            aria-describedby={`${id}-review-help`}
            onChange={(event) => {
              setReviewBy(event.target.value);
            }}
            className={fieldClass}
          />
          <p id={`${id}-review-help`} className="mt-1 text-xs text-slate-600">
            At most 365 days ahead. After this date the decision no longer counts until it is
            renewed.
          </p>
        </div>
      )}
      {errors.length > 0 && (
        <ul role="alert" className="list-disc space-y-1 pl-5 text-sm text-red-900">
          {errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={sending} className={buttonClass}>
          {sending ? "Sending…" : "Propose change"}
        </button>
        <button type="button" onClick={onCancel} disabled={sending} className={secondaryClass}>
          Cancel
        </button>
      </div>
    </form>
  );
}
