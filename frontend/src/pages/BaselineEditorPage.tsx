import { useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  MAX_SOURCE_BYTES,
  assignPolicy,
  getVersion,
  savePolicy,
  validatePolicy,
  type PolicyError,
} from "../api/policies";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, linkClass, secondaryClass } from "../components/styles";
import {
  appliedMessage,
  DestinationPicker,
  destinationRequest,
  type Destination,
} from "../policies/AssignForm";
import {
  baselineHref,
  canWritePolicies,
  CHANGES_VIEW,
  GOVERNANCE,
  proposedMessage,
  utf8Bytes,
} from "../policies/format";
import { RuleForm } from "../policies/RuleForm";
import {
  emptyBaseline,
  formProblems,
  fromDocument,
  recommendedBaseline,
  toYaml,
  type BaselineForm,
} from "../policies/yaml";

const errorClass = "rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900";
const okClass = "rounded border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900";

/** Shown on the baseline page after saving, via router state. */
export interface SavedState {
  message: string;
}

type Feedback =
  | { kind: "problems"; problems: string[] }
  | { kind: "errors"; errors: PolicyError[] }
  | { kind: "valid"; name: string; rules: string[] }
  | { kind: "failed"; message: string };

function FeedbackBox({ feedback }: { feedback: Feedback }) {
  switch (feedback.kind) {
    case "problems":
      return (
        <div className={errorClass}>
          <p className="font-medium">Fix these before saving:</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {feedback.problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      );
    case "errors":
      return (
        <div className={errorClass}>
          <p className="font-medium">
            {`Not valid: ${String(feedback.errors.length)} ${feedback.errors.length === 1 ? "problem" : "problems"}.`}
          </p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {feedback.errors.map((error, index) => (
              <li key={`${error.path}-${String(index)}`}>
                <code className="font-mono">{error.path}</code>
                {`: ${error.message}`}
              </li>
            ))}
          </ul>
        </div>
      );
    case "valid":
      return (
        <div className={okClass}>
          {`Valid. Baseline ${feedback.name} with rules: ${feedback.rules.join(", ")}. Nothing is saved yet.`}
        </div>
      );
    case "failed":
      return <div className={errorClass}>{feedback.message}</div>;
  }
}

function YamlHelp() {
  return (
    <details className="text-sm text-slate-700">
      <summary className="cursor-pointer font-medium">Field reference</summary>
      <ul className="mt-2 list-disc space-y-1 pl-5">
        <li>
          <code className="font-mono">metadata.name</code> identifies the baseline: a new name
          creates a baseline, an existing name adds its next version.
        </li>
        <li>
          <code className="font-mono">spec.scope.groups</code> is{" "}
          <code className="font-mono">[&quot;*&quot;]</code> for everywhere it is applied, or group
          paths; a group also covers its subgroups.
        </li>
        <li>
          Rule <code className="font-mono">gitlab.project.visibility</code> with{" "}
          <code className="font-mono">allowed</code>: any of private, internal, public.
        </li>
        <li>
          Rule <code className="font-mono">gitlab.branch.default_protected</code> with{" "}
          <code className="font-mono">required: true</code>.
        </li>
        <li>
          Rule <code className="font-mono">gitlab.project.merge_request_approvals</code> with{" "}
          <code className="font-mono">min_approvals</code> (0 to 20) and optional{" "}
          <code className="font-mono">forbid_author_approval</code>,{" "}
          <code className="font-mono">forbid_committer_approval</code>,{" "}
          <code className="font-mono">reset_approvals_on_push</code> and{" "}
          <code className="font-mono">forbid_override</code> (true or false).
        </li>
        <li>
          Rule <code className="font-mono">gitlab.project.privileged_members</code> with{" "}
          <code className="font-mono">max_privileged</code> (1 to 1000) and optional{" "}
          <code className="font-mono">forbid_inactive_accounts</code>.
        </li>
        <li>
          Rule <code className="font-mono">gitlab.group.two_factor_required</code> with optional{" "}
          <code className="font-mono">max_grace_hours</code> (0 to 720).
        </li>
        <li>
          Rule <code className="font-mono">gitlab.project.access_tokens</code> with{" "}
          <code className="font-mono">max_lifetime_days</code> and optional{" "}
          <code className="font-mono">max_unused_days</code> (1 to 365 each).
        </li>
        <li>
          Each rule has a unique <code className="font-mono">id</code> and an optional{" "}
          <code className="font-mono">severity</code> (low, medium, high or critical) and{" "}
          <code className="font-mono">description</code>.
        </li>
      </ul>
    </details>
  );
}

function Editor({
  initialForm,
  initialYaml,
  existing,
}: {
  initialForm: BaselineForm | null;
  initialYaml: string;
  /** The baseline a new version is being made of; its name is fixed. */
  existing: { name: string; version: number } | null;
}) {
  const { session, refresh } = useAuth();
  const navigate = useNavigate();
  const id = useId();
  const [form, setForm] = useState<BaselineForm>(initialForm ?? emptyBaseline());
  // YAML mode when the form can't show the version, or when asked for; the form is the default.
  const [yaml, setYaml] = useState<string | null>(initialForm ? null : initialYaml);
  const [destination, setDestination] = useState<Destination>({ type: "instance", picked: null });
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [busy, setBusy] = useState<"validate" | "save" | "apply" | null>(null);
  const result = useRef<HTMLDivElement>(null);

  const source = yaml ?? toYaml(form);
  const bytes = utf8Bytes(source);
  const tooLarge = bytes > MAX_SOURCE_BYTES;

  useEffect(() => {
    if (feedback) result.current?.focus();
  }, [feedback]);

  function failed(error: unknown) {
    const apiError =
      error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
    setFeedback({ kind: "failed", message: apiError.message });
    if (apiError.status === 401) void refresh();
  }

  async function validate() {
    if (session.status !== "signed_in") return;
    setBusy("validate");
    try {
      const checked = await validatePolicy(source, session.user.csrf_token);
      setFeedback(
        checked.valid
          ? { kind: "valid", name: checked.name ?? "", rules: checked.rule_ids }
          : { kind: "errors", errors: checked.errors },
      );
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
    }
  }

  async function save(apply: boolean) {
    if (session.status !== "signed_in") return;
    if (yaml === null) {
      const problems = formProblems(form);
      if (problems.length > 0) {
        setFeedback({ kind: "problems", problems });
        return;
      }
    }
    setBusy(apply ? "apply" : "save");
    try {
      const saved = await savePolicy(source, session.user.csrf_token);
      if ("errors" in saved) {
        setFeedback({ kind: "errors", errors: saved.errors });
        return;
      }
      if ("proposed" in saved) {
        const message = `${proposedMessage(saved.proposed)}${
          apply ? " Apply it once it is approved." : ""
        }`;
        const state: SavedState = { message };
        void navigate(CHANGES_VIEW, { state });
        return;
      }
      const { name, version } = saved.version;
      const parts = [
        saved.created
          ? `Saved version ${String(version)} of ${name}.`
          : `This is identical to version ${String(version)} of ${name}, so no new version was saved.`,
      ];
      if (apply) {
        const request = destinationRequest(name, version, destination);
        if (typeof request === "string") {
          parts.push(`It isn't applied anywhere yet: ${request}`);
        } else {
          try {
            parts.push(appliedMessage(await assignPolicy(request, session.user.csrf_token)));
          } catch (error) {
            const apiError =
              error instanceof ApiError
                ? error
                : new ApiError(0, "The response could not be read.");
            parts.push(`Applying it failed: ${apiError.message}`);
          }
        }
      } else if (!existing) {
        parts.push("It isn't applied anywhere yet. Use Apply on this page to start checking it.");
      }
      const state: SavedState = { message: parts.join(" ") };
      void navigate(baselineHref(name, version), { state });
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
    }
  }

  const disabled = busy !== null || !source.trim() || tooLarge;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Editing mode">
        <button
          type="button"
          aria-pressed={yaml === null}
          disabled={yaml === null || initialForm === null}
          onClick={() => {
            setYaml(null);
            setFeedback(null);
          }}
          className={secondaryClass}
        >
          Form
        </button>
        <button
          type="button"
          aria-pressed={yaml !== null}
          disabled={yaml !== null}
          onClick={() => {
            setYaml(toYaml(form));
            setFeedback(null);
          }}
          className={secondaryClass}
        >
          Edit as YAML (advanced)
        </button>
      </div>
      {yaml !== null && initialForm !== null && (
        <p className="text-sm text-slate-600">Going back to the form discards changes made here.</p>
      )}
      {yaml !== null && initialForm === null && existing && (
        <p className="text-sm text-slate-600">
          This version uses settings the form can&apos;t show, so it opens as YAML.
        </p>
      )}

      {yaml === null ? (
        <RuleForm form={form} onChange={setForm} nameLocked={existing !== null} />
      ) : (
        <div className="space-y-2">
          <label className="block text-sm font-medium" htmlFor={`${id}-source`}>
            Baseline YAML
          </label>
          <YamlHelp />
          <textarea
            id={`${id}-source`}
            aria-describedby={`${id}-size`}
            value={yaml}
            onChange={(event) => {
              setYaml(event.target.value);
              setFeedback(null);
            }}
            rows={22}
            spellCheck={false}
            className="block w-full rounded border border-slate-300 bg-white p-2 font-mono text-sm focus-visible:outline-2 focus-visible:outline-sky-600"
          />
          <p
            id={`${id}-size`}
            className={`text-sm ${tooLarge ? "text-red-900" : "text-slate-600"}`}
          >
            {`${String(bytes)} of ${String(MAX_SOURCE_BYTES)} bytes${tooLarge ? ": too large to save" : ""}.`}
          </p>
        </div>
      )}

      {!existing && (
        <section
          aria-labelledby={`${id}-apply`}
          className="space-y-3 rounded border border-slate-200 bg-white p-4"
        >
          <h2 id={`${id}-apply`} className="text-base font-semibold">
            Where to check it
          </h2>
          <DestinationPicker value={destination} onChange={setDestination} />
        </section>
      )}

      <div ref={result} tabIndex={-1} aria-live="polite" className="focus:outline-none">
        {feedback && <FeedbackBox feedback={feedback} />}
      </div>

      <div className="flex flex-wrap gap-2">
        {existing ? (
          <button
            type="button"
            onClick={() => void save(false)}
            disabled={disabled}
            className={buttonClass}
          >
            {busy === "save" ? "Saving…" : "Save new version"}
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => void save(true)}
              disabled={disabled}
              className={buttonClass}
            >
              {busy === "apply" ? "Saving and applying…" : "Save and apply"}
            </button>
            <button
              type="button"
              onClick={() => void save(false)}
              disabled={disabled}
              className={secondaryClass}
            >
              {busy === "save" ? "Saving…" : "Save without applying"}
            </button>
          </>
        )}
        {yaml !== null && (
          <button
            type="button"
            onClick={() => void validate()}
            disabled={disabled}
            className={secondaryClass}
          >
            {busy === "validate" ? "Checking…" : "Check YAML"}
          </button>
        )}
        <Link
          to={existing ? baselineHref(existing.name) : GOVERNANCE}
          className={`${secondaryClass} inline-block`}
        >
          Cancel
        </Link>
      </div>
    </div>
  );
}

function FromVersion({ name, version }: { name: string; version: number }) {
  const loaded = useLoad(`policy:${name}@${String(version)}`, (signal) =>
    getVersion(name, version, signal),
  );
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) {
    return (
      <p role="status" className="text-slate-600">
        Loading version…
      </p>
    );
  }
  return (
    <Editor
      key={`${name}@${String(version)}`}
      initialForm={fromDocument(loaded.data.document)}
      initialYaml={loaded.data.source_yaml}
      existing={{ name, version }}
    />
  );
}

export function BaselineEditorPage() {
  const { session } = useAuth();
  const [params] = useSearchParams();
  const canWrite = canWritePolicies(session.status === "signed_in" ? session.user : null);
  const from = params.get("from");
  const version = Number(params.get("version"));
  const editing = from !== null && Number.isInteger(version) && version > 0;
  const recommended = params.get("start") === "recommended";
  const yamlFirst = params.get("mode") === "yaml";

  const title = editing
    ? `Edit ${from}`
    : recommended
      ? "Use the recommended baseline"
      : yamlFirst
        ? "Import a baseline as YAML"
        : "Create a baseline";
  const intro = editing
    ? `Starting from version ${String(version)}. Saving adds a new version; places that use version ${String(version)} keep it until you apply the new one.`
    : "Choose the rules and where to check them. Nothing is saved until you save, and saving is audited.";

  let body;
  if (!canWrite) {
    body = (
      <p role="alert" className={errorClass}>
        You do not have permission to change baselines. Analysts and administrators can.
      </p>
    );
  } else if (editing) {
    body = <FromVersion name={from} version={version} />;
  } else if (yamlFirst) {
    body = <Editor initialForm={null} initialYaml="" existing={null} />;
  } else {
    body = (
      <Editor
        initialForm={recommended ? recommendedBaseline() : emptyBaseline()}
        initialYaml=""
        existing={null}
      />
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <p className="text-sm">
          <Link to={GOVERNANCE} className={linkClass}>
            Governance
          </Link>
          {editing && (
            <>
              {" › "}
              <Link to={baselineHref(from)} className={linkClass}>
                {from}
              </Link>
            </>
          )}
        </p>
        <h1 id="page-title" className="mt-1 text-2xl font-semibold">
          {title}
        </h1>
        <p className="mt-2 max-w-3xl text-slate-600">{intro}</p>
      </div>
      {body}
    </section>
  );
}
