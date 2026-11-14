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
import { PageHeader } from "../ui/PageHeader";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";

const errorClass = "alert alert-danger text-body";
const okClass = "alert alert-success text-body";

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
          <p className="fw-medium">Fix these before saving:</p>
          <ul className="mt-1 ps-3">
            {feedback.problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      );
    case "errors":
      return (
        <div className={errorClass}>
          <p className="fw-medium">
            {`Not valid: ${String(feedback.errors.length)} ${feedback.errors.length === 1 ? "problem" : "problems"}.`}
          </p>
          <ul className="mt-1 ps-3">
            {feedback.errors.map((error, index) => (
              <li key={`${error.path}-${String(index)}`}>
                <code className="font-monospace">{error.path}</code>
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
    <details className="text-secondary">
      <summary className="fw-medium">Field reference</summary>
      <ul className="mt-2 ps-3">
        <li>
          <code className="font-monospace">metadata.name</code> identifies the baseline: a new name
          creates a baseline, an existing name adds its next version.
        </li>
        <li>
          <code className="font-monospace">spec.scope.groups</code> is{" "}
          <code className="font-monospace">[&quot;*&quot;]</code> for everywhere it is applied, or
          group paths; a group also covers its subgroups.
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.project.visibility</code> with{" "}
          <code className="font-monospace">allowed</code>: any of private, internal, public.
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.branch.default_protected</code> with{" "}
          <code className="font-monospace">required: true</code>.
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.project.merge_request_approvals</code> with{" "}
          <code className="font-monospace">min_approvals</code> (0 to 20) and optional{" "}
          <code className="font-monospace">forbid_author_approval</code>,{" "}
          <code className="font-monospace">forbid_committer_approval</code>,{" "}
          <code className="font-monospace">reset_approvals_on_push</code> and{" "}
          <code className="font-monospace">forbid_override</code> (true or false).
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.project.privileged_members</code> with{" "}
          <code className="font-monospace">max_privileged</code> (1 to 1000) and optional{" "}
          <code className="font-monospace">forbid_inactive_accounts</code>.
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.group.two_factor_required</code> with
          optional <code className="font-monospace">max_grace_hours</code> (0 to 720).
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.project.access_tokens</code> with{" "}
          <code className="font-monospace">max_lifetime_days</code> and optional{" "}
          <code className="font-monospace">max_unused_days</code> (1 to 365 each).
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.change.reviewed</code> with optional{" "}
          <code className="font-monospace">window_days</code> (1 to 365, default 90),{" "}
          <code className="font-monospace">min_approvals</code> (1 to 20, default 1),{" "}
          <code className="font-monospace">forbid_self_approval</code> (default true) and{" "}
          <code className="font-monospace">allow_direct_push</code> (default false).
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.change.pipeline_passed</code> with optional{" "}
          <code className="font-monospace">window_days</code> and{" "}
          <code className="font-monospace">require_tests</code> (default false).
        </li>
        <li>
          Rule <code className="font-monospace">gitlab.change.deployed_after_review</code> with
          optional <code className="font-monospace">window_days</code>.
        </li>
        <li>
          Each rule has a unique <code className="font-monospace">id</code> and an optional{" "}
          <code className="font-monospace">severity</code> (low, medium, high or critical) and{" "}
          <code className="font-monospace">description</code>.
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
    <div>
      <div className="d-flex flex-wrap gap-2" role="group" aria-label="Editing mode">
        <button
          type="button"
          aria-pressed={yaml === null}
          disabled={yaml === null || initialForm === null}
          onClick={() => {
            setYaml(null);
            setFeedback(null);
          }}
          className="btn"
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
          className="btn"
        >
          Edit as YAML (advanced)
        </button>
      </div>
      {yaml !== null && initialForm !== null && (
        <p className="text-secondary">Going back to the form discards changes made here.</p>
      )}
      {yaml !== null && initialForm === null && existing && (
        <p className="text-secondary">
          This version uses settings the form can&apos;t show, so it opens as YAML.
        </p>
      )}

      {yaml === null ? (
        <RuleForm form={form} onChange={setForm} nameLocked={existing !== null} />
      ) : (
        <div>
          <label className="d-block fw-medium" htmlFor={`${id}-source`}>
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
            className="form-control font-monospace"
          />
          <p id={`${id}-size`} className={tooLarge ? "text-danger-emphasis" : "text-secondary"}>
            {`${String(bytes)} of ${String(MAX_SOURCE_BYTES)} bytes${tooLarge ? ": too large to save" : ""}.`}
          </p>
        </div>
      )}

      {!existing && (
        <section aria-labelledby={`${id}-apply`} className="card card-body mb-3">
          <h2 id={`${id}-apply`} className="fw-semibold">
            Where to check it
          </h2>
          <DestinationPicker value={destination} onChange={setDestination} />
        </section>
      )}

      <div ref={result} tabIndex={-1} aria-live="polite" className="focus:outline-none">
        {feedback && <FeedbackBox feedback={feedback} />}
      </div>

      <div className="d-flex flex-wrap gap-2">
        {existing ? (
          <button
            type="button"
            onClick={() => void save(false)}
            disabled={disabled}
            className="btn btn-primary"
          >
            {busy === "save" ? "Saving…" : "Save new version"}
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => void save(true)}
              disabled={disabled}
              className="btn btn-primary"
            >
              {busy === "apply" ? "Saving and applying…" : "Save and apply"}
            </button>
            <button
              type="button"
              onClick={() => void save(false)}
              disabled={disabled}
              className="btn"
            >
              {busy === "save" ? "Saving…" : "Save without applying"}
            </button>
          </>
        )}
        {yaml !== null && (
          <button type="button" onClick={() => void validate()} disabled={disabled} className="btn">
            {busy === "validate" ? "Checking…" : "Check YAML"}
          </button>
        )}
        <Link to={existing ? baselineHref(existing.name) : GOVERNANCE} className="btn">
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
      <p role="status" className="text-secondary">
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
  const device = useDeviceClass();
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
  if (device === "phone") {
    body = <EmptyState title="Editing a baseline is available on a larger screen." />;
  } else if (!canWrite) {
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
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={
          <>
            <Link to={GOVERNANCE}>Governance</Link>
            {editing && (
              <>
                {" › "}
                <Link to={baselineHref(from)}>{from}</Link>
              </>
            )}
          </>
        }
        title={title}
        subtitle={intro}
      />
      {body}
    </section>
  );
}
