import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import {
  CAN_PROPOSE,
  getTaxonomy,
  MAX_JUSTIFICATION,
  proposeTaxonomy,
  validateTaxonomy,
  type TaxonomyIssue,
  type TaxonomyLabel,
  type TaxonomyVersion,
  type TaxonomyView,
} from "../api/labels";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  emptyTaxonomy,
  formFromSpec,
  isHexColor,
  LABELS,
  parseAliases,
  taxonomyProblems,
  taxonomyYaml,
  type TaxonomyForm,
} from "../labels/format";
import { Swatch } from "../labels/Swatch";
import { CHANGES_VIEW } from "../policies/format";
import { hasPermission } from "../remediation/format";
import { PageHeader } from "../ui/PageHeader";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";

const DEFAULT_COLOR = "#1f75cb";

/** Each label row with the aliases as typed, so a trailing comma survives typing. */
interface Row {
  key: number;
  label: TaxonomyLabel;
  aliasText: string;
}

function rowsOf(labels: TaxonomyLabel[]): Row[] {
  return labels.map((label, index) => ({
    key: index,
    label: { ...label },
    aliasText: label.aliases.join(", "),
  }));
}

function LabelRow({
  row,
  index,
  onChange,
  onRemove,
}: {
  row: Row;
  index: number;
  onChange: (row: Row) => void;
  onRemove: () => void;
}) {
  const id = useId();
  const { label } = row;
  const title = label.name.trim() || `Label ${String(index + 1)}`;
  const color = label.color.trim();
  function set(change: Partial<TaxonomyLabel>) {
    onChange({ ...row, label: { ...label, ...change } });
  }
  return (
    <fieldset className="rounded border p-3">
      <legend className="px-1 fw-medium">{title}</legend>
      <div className="grid gap-3 md:grid-cols-2">
        <label className="fw-medium" htmlFor={`${id}-name`}>
          Name
          <input
            id={`${id}-name`}
            value={label.name}
            onChange={(event) => {
              set({ name: event.target.value });
            }}
            className="form-control mt-1"
          />
        </label>
        <div className="fw-medium">
          <label htmlFor={`${id}-hex`}>Colour (#rrggbb)</label>
          <div className="mt-1 d-flex align-items-center gap-2">
            <input
              type="color"
              aria-label={`Pick the colour of ${title}`}
              value={isHexColor(color) ? color.toLowerCase() : DEFAULT_COLOR}
              onChange={(event) => {
                set({ color: event.target.value });
              }}
              className="h-8 w-10 rounded border"
            />
            <input
              id={`${id}-hex`}
              value={label.color}
              onChange={(event) => {
                set({ color: event.target.value });
              }}
              aria-invalid={!isHexColor(color)}
              className="form-control font-monospace"
            />
          </div>
          <p className="mt-1 fw-normal">
            <Swatch color={isHexColor(color) ? color : null} />
          </p>
        </div>
        <label className="fw-medium" htmlFor={`${id}-description`}>
          Description
          <input
            id={`${id}-description`}
            value={label.description ?? ""}
            onChange={(event) => {
              set({ description: event.target.value || null });
            }}
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium" htmlFor={`${id}-aliases`}>
          Rename from (aliases, comma-separated)
          <input
            id={`${id}-aliases`}
            value={row.aliasText}
            onChange={(event) => {
              onChange({
                ...row,
                aliasText: event.target.value,
                label: { ...label, aliases: parseAliases(event.target.value) },
              });
            }}
            className="form-control mt-1"
          />
        </label>
      </div>
      <button type="button" onClick={onRemove} className="btn">
        {`Remove ${title}`}
      </button>
    </fieldset>
  );
}

function Issues({ issues }: { issues: TaxonomyIssue[] }) {
  return (
    <ul className="ps-3">
      {issues.map((issue) => (
        <li key={`${issue.path}:${issue.message}`}>
          {issue.path ? `${issue.path}: ${issue.message}` : issue.message}
        </li>
      ))}
    </ul>
  );
}

type Outcome = { kind: "saved"; version: TaxonomyVersion } | null;

function Editor({ view }: { view: TaxonomyView }) {
  const { session, refresh } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const id = useId();
  const start = view.current?.spec
    ? formFromSpec(view.current.name, view.current.spec)
    : emptyTaxonomy();
  const [mode, setMode] = useState<"form" | "yaml">("form");
  const [name, setName] = useState(start.name);
  const [groupText, setGroupText] = useState(start.groups.join(", "));
  const [strict, setStrict] = useState(start.strict);
  const [rows, setRows] = useState<Row[]>(rowsOf(start.labels));
  const [nextKey, setNextKey] = useState(start.labels.length);
  const [yaml, setYaml] = useState(view.current?.source_yaml ?? taxonomyYaml(start));
  const [justification, setJustification] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [issues, setIssues] = useState<TaxonomyIssue[]>([]);
  const [checked, setChecked] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const status = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (outcome || problems.length > 0 || issues.length > 0 || failure) status.current?.focus();
  }, [outcome, problems, issues, failure]);

  const form: TaxonomyForm = {
    name: name.trim(),
    groups: parseAliases(groupText),
    strict,
    labels: rows.map((row) => row.label),
  };

  function fail(error: unknown) {
    const apiError =
      error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
    setFailure(apiError.message);
    if (apiError.status === 401) void refresh();
  }

  async function check(source: string): Promise<boolean> {
    if (!user) return false;
    const result = await validateTaxonomy(source, user.csrf_token);
    setIssues(result.errors);
    setChecked(result.valid ? "The taxonomy is valid." : null);
    return result.valid;
  }

  async function onCheck() {
    setFailure(null);
    setProblems([]);
    try {
      await check(yaml);
    } catch (error) {
      fail(error);
    }
  }

  async function backToForm() {
    if (!user) return;
    setFailure(null);
    try {
      const result = await validateTaxonomy(yaml, user.csrf_token);
      setIssues(result.errors);
      if (!result.valid || !result.spec) return;
      const next = formFromSpec(result.name ?? name, result.spec);
      setName(next.name);
      setGroupText(next.groups.join(", "));
      setStrict(next.strict);
      setRows(rowsOf(next.labels));
      setNextKey(next.labels.length);
      setMode("form");
    } catch (error) {
      fail(error);
    }
  }

  function showYaml() {
    setYaml(taxonomyYaml(form));
    setIssues([]);
    setChecked(null);
    setMode("yaml");
  }

  async function propose(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user) return;
    setFailure(null);
    setChecked(null);
    let source = yaml;
    if (mode === "form") {
      const found = taxonomyProblems(form);
      setProblems(found);
      if (found.length > 0) return;
      source = taxonomyYaml(form);
    } else {
      setProblems([]);
    }
    setSending(true);
    try {
      if (!(await check(source))) return;
      setChecked(null);
      const version = await proposeTaxonomy(source, justification, user.csrf_token);
      setOutcome({ kind: "saved", version });
    } catch (error) {
      fail(error);
    } finally {
      setSending(false);
    }
  }

  if (outcome) {
    const { version } = outcome;
    const inForce = version.state === "approved";
    return (
      <div>
        <p ref={status} tabIndex={-1} role="status" className="alert alert-success text-body">
          {inForce
            ? `Saved ${version.name} v${String(version.version)}. Review is off, so it is in force now and was recorded as self-approved.`
            : `Proposed ${version.name} v${String(version.version)}. It takes effect once someone other than you approves it.`}
        </p>
        <p className="d-flex flex-wrap gap-3">
          <Link to={LABELS} className="btn btn-primary">
            Back to labels
          </Link>
          {!inForce && (
            <Link to={CHANGES_VIEW} className="btn">
              Open the review queue
            </Link>
          )}
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={(event) => void propose(event)} noValidate>
      {(problems.length > 0 || issues.length > 0 || failure || checked) && (
        <div
          role={checked && !failure ? "status" : "alert"}
          className={`alert text-body ${
            checked && !failure && problems.length === 0 && issues.length === 0
              ? "alert-success"
              : "alert-danger"
          }`}
        >
          <p ref={status} tabIndex={-1} className="fw-medium">
            {failure ??
              (problems.length > 0 || issues.length > 0
                ? "Fix these before proposing the taxonomy:"
                : checked)}
          </p>
          {problems.length > 0 && (
            <ul className="mt-1 ps-3">
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          )}
          {issues.length > 0 && <Issues issues={issues} />}
        </div>
      )}
      <div className="d-flex gap-2" role="group" aria-label="Edit as">
        <button
          type="button"
          aria-pressed={mode === "form"}
          onClick={() => void backToForm()}
          disabled={mode === "form"}
          className="btn"
        >
          Form
        </button>
        <button
          type="button"
          aria-pressed={mode === "yaml"}
          onClick={showYaml}
          disabled={mode === "yaml"}
          className="btn"
        >
          YAML
        </button>
      </div>
      {mode === "form" ? (
        <div>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="fw-medium" htmlFor={`${id}-name`}>
              Taxonomy name
              <input
                id={`${id}-name`}
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                }}
                className="form-control mt-1"
              />
            </label>
            <label className="fw-medium" htmlFor={`${id}-groups`}>
              Groups (paths or patterns, comma-separated; * for all)
              <input
                id={`${id}-groups`}
                value={groupText}
                onChange={(event) => {
                  setGroupText(event.target.value);
                }}
                className="form-control mt-1"
              />
            </label>
          </div>
          <p className="text-secondary">
            Labels are expected on each top-level group, so every project below inherits them.
          </p>
          <label className="d-flex align-items-start gap-2">
            <input
              type="checkbox"
              checked={strict}
              onChange={(event) => {
                setStrict(event.target.checked);
              }}
              className="mt-1"
            />
            <span>
              <span className="fw-medium">Strict</span>: labels not in the taxonomy count as drift.
            </span>
          </label>
          <div>
            <h2 className="card-title">{`Labels (${String(rows.length)})`}</h2>
            {rows.map((row, index) => (
              <LabelRow
                key={row.key}
                row={row}
                index={index}
                onChange={(next) => {
                  setRows(rows.map((item) => (item.key === row.key ? next : item)));
                }}
                onRemove={() => {
                  setRows(rows.filter((item) => item.key !== row.key));
                }}
              />
            ))}
            <button
              type="button"
              onClick={() => {
                setRows([
                  ...rows,
                  {
                    key: nextKey,
                    label: { name: "", color: DEFAULT_COLOR, description: null, aliases: [] },
                    aliasText: "",
                  },
                ]);
                setNextKey(nextKey + 1);
              }}
              className="btn"
            >
              Add a label
            </button>
          </div>
        </div>
      ) : (
        <div>
          <label className="fw-medium" htmlFor={`${id}-yaml`}>
            Taxonomy YAML
          </label>
          <textarea
            id={`${id}-yaml`}
            value={yaml}
            rows={20}
            spellCheck={false}
            onChange={(event) => {
              setYaml(event.target.value);
              setChecked(null);
            }}
            className="form-control mt-1 font-monospace small"
          />
          <button type="button" onClick={() => void onCheck()} className="btn">
            Check the YAML
          </button>
        </div>
      )}
      <label className="d-block fw-medium" htmlFor={`${id}-why`}>
        Justification (optional)
        <textarea
          id={`${id}-why`}
          value={justification}
          maxLength={MAX_JUSTIFICATION}
          rows={3}
          onChange={(event) => {
            setJustification(event.target.value);
          }}
          className="form-control mt-1"
        />
      </label>
      <p className="text-secondary">
        {view.review_required
          ? "Review is required: someone other than you must approve this before it takes effect."
          : "Review is off: this takes effect at once and is recorded as self-approved."}
      </p>
      <div className="d-flex flex-wrap gap-3">
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Proposing…" : "Propose the taxonomy"}
        </button>
        <Link to={LABELS} className="btn">
          Cancel
        </Link>
      </div>
    </form>
  );
}

export function TaxonomyEditorPage() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const canPropose = user !== null && hasPermission(user, CAN_PROPOSE);
  const device = useDeviceClass();
  const view = useLoad("labels:taxonomy:edit", (signal) => getTaxonomy(null, signal));

  let body;
  if (device === "phone") {
    body = <EmptyState title="Proposing a taxonomy is available on a larger screen." />;
  } else if (!canPropose) {
    body = (
      <p className="text-secondary">
        Proposing a taxonomy needs the baseline writing permission, which admins and analysts have.
      </p>
    );
  } else if (view.error) body = <ErrorMessage error={view.error} retry={view.retry} />;
  else if (!view.data) {
    body = <Loading label="Loading the taxonomy in force…" />;
  } else body = <Editor view={view.data} />;

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to={LABELS}>Labels</Link>}
        title="Propose a label taxonomy"
        subtitle="The labels every group should have: name, colour, description, and the old names to rename from. Names match exactly; an alias, or the same name in another case or spacing, counts as misnamed."
      />
      {body}
    </section>
  );
}
