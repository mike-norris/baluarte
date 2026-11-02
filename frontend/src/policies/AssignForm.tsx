import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import {
  assignPolicy,
  type Applied,
  type AssignmentRequest,
  type Proposed,
  type TargetType,
  type VersionSummary,
} from "../api/policies";
import { useAuth } from "../auth/context";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { proposedMessage, targetText, targetTypeLabels } from "./format";
import { TargetPicker, type Picked } from "./TargetPicker";

const errorClass = "rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900";
const targetTypes: readonly TargetType[] = ["instance", "group", "project"];

export interface Destination {
  type: TargetType;
  picked: Picked | null;
}

/** The request for ``destination``, or a problem to show when a group or project is unpicked. */
export function destinationRequest(
  name: string,
  version: number,
  destination: Destination,
): AssignmentRequest | string {
  if (destination.type === "instance") {
    return { policy_name: name, version, target_type: "instance" };
  }
  if (destination.picked?.type !== destination.type) {
    return `Search for and select a ${destination.type}.`;
  }
  return {
    policy_name: name,
    version,
    target_type: destination.type,
    target_id: destination.picked.gitlabId,
  };
}

/** "Applied … Checking now.", "… was already applied …", or that it waits for review. */
export function appliedMessage(applied: Applied | Proposed): string {
  if ("proposed" in applied) return proposedMessage(applied.proposed);
  const { assignment } = applied;
  const what = `${assignment.policy_name} v${String(assignment.version)}`;
  return applied.created
    ? `Applied ${what} to ${targetText(assignment)}. Checking now; results appear in a minute or two.`
    : `${what} was already applied to ${targetText(assignment)}. Nothing changed.`;
}

/** Where to apply a baseline: the whole instance, or a group or project found by path. */
export function DestinationPicker({
  value,
  onChange,
  legend = "Apply to",
}: {
  value: Destination;
  onChange: (destination: Destination) => void;
  legend?: string;
}) {
  const id = useId();
  return (
    <div className="space-y-3">
      <fieldset>
        <legend className="text-sm font-medium">{legend}</legend>
        <div className="mt-1 flex flex-wrap gap-4">
          {targetTypes.map((type) => (
            <label key={type} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name={`${id}-type`}
                value={type}
                checked={value.type === type}
                onChange={() => {
                  onChange({ type, picked: value.picked?.type === type ? value.picked : null });
                }}
              />
              {type === "instance" ? "Whole instance" : `A ${targetTypeLabels[type].toLowerCase()}`}
            </label>
          ))}
        </div>
      </fieldset>
      {value.type === "instance" ? (
        <p className="text-sm text-slate-600">
          Every group and project is checked, except where a closer setting replaces it.
        </p>
      ) : (
        <TargetPicker
          key={value.type}
          type={value.type}
          value={value.picked}
          onChange={(picked) => {
            onChange({ ...value, picked });
          }}
        />
      )}
    </div>
  );
}

/** Apply one version of a baseline to a target. Applying another version to it replaces it. */
export function ApplyForm({
  name,
  versions,
  onApplied,
  onCancel,
}: {
  name: string;
  /** Newest first. */
  versions: VersionSummary[];
  onApplied: (applied: Applied | Proposed) => void;
  onCancel: () => void;
}) {
  const { session, refresh } = useAuth();
  const id = useId();
  const [version, setVersion] = useState<number | null>(versions[0]?.version ?? null);
  const [destination, setDestination] = useState<Destination>({ type: "instance", picked: null });
  const [problems, setProblems] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const alert = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (problems.length > 0) alert.current?.focus();
  }, [problems]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (session.status !== "signed_in") return;
    if (version === null) {
      setProblems(["Choose a version."]);
      return;
    }
    const request = destinationRequest(name, version, destination);
    if (typeof request === "string") {
      setProblems([request]);
      return;
    }
    setProblems([]);
    setSending(true);
    try {
      onApplied(await assignPolicy(request, session.user.csrf_token));
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setProblems([apiError.message]);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-labelledby={`${id}-title`}
      noValidate
      className="space-y-4 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id={`${id}-title`} className="text-lg font-semibold">
        {`Apply ${name}`}
      </h2>
      <p className="text-sm text-slate-600">
        Closer settings replace broader ones: a project&apos;s own setting, then its groups, then
        the whole instance. Applying another version where this baseline is already applied replaces
        it there.
      </p>
      {problems.length > 0 && (
        <div ref={alert} role="alert" tabIndex={-1} className={errorClass}>
          <ul className="list-disc pl-5">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      )}
      <label className="block max-w-xs text-sm font-medium">
        Version
        <select
          value={version ?? ""}
          onChange={(event) => {
            setVersion(Number(event.target.value));
          }}
          className={fieldClass}
        >
          {versions.map((item, index) => (
            <option key={item.version} value={item.version}>
              {`Version ${String(item.version)}${index === 0 ? " (latest)" : ""}`}
            </option>
          ))}
        </select>
      </label>
      <DestinationPicker value={destination} onChange={setDestination} />
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={sending} className={buttonClass}>
          {sending ? "Applying…" : "Apply"}
        </button>
        <button type="button" onClick={onCancel} className={secondaryClass}>
          Cancel
        </button>
      </div>
    </form>
  );
}
