import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import {
  createChangeSet,
  decideChangeSet,
  deleteSavedSelection,
  getChangeSet,
  listChangeSets,
  listSavedSelections,
  previewSelection,
  saveSelection,
  type ChangeSet,
  type ChangeSetState,
  type ChangeSetSummary,
  type ChangeTarget,
  type DriftFilter,
  type Selection,
  type SelectionPreview,
  type SkipCode,
  type WarningCode,
} from "../api/changeSets";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { failureText, settingLabels, valueText } from "../templates/format";
import { Badge, type Tone } from "../ui/Badge";
import { DigestDisplay } from "../ui/DigestDisplay";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

const stateTones: Record<ChangeSetState, Tone> = {
  proposed: "info",
  approved: "success",
  rejected: "danger",
  withdrawn: "secondary",
  expired: "secondary",
};

export const warningLabels: Record<WarningCode, string> = {
  weakens_protection: "Weakens protection",
  removes_access: "Removes access",
  disrupts_work: "Disrupts work",
};

export const skipLabels: Record<SkipCode, string> = {
  unsupported: "Not on this provider",
  unknown: "Cannot tell the current value",
  excepted: "Exception approved",
  conflicts_with_baseline: "Conflicts with a baseline",
  label_plans: "Changed by label plans",
};

function usePermissions() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const permissions = user?.permissions ?? [];
  return {
    token: user?.csrf_token ?? null,
    userId: user?.user_id ?? null,
    canPlan: permissions.includes("remediation:plan"),
    canApprove: permissions.includes("remediation:approve"),
  };
}

function list(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function settingName(key: string): string {
  return settingLabels[key] ?? key;
}

// --- selection ---

interface SelectionForm {
  groups: string;
  projects: string;
  tags: string;
  template: string;
  drift: DriftFilter[];
  findings: "" | "open" | "none";
  policy: string;
}

const EMPTY: SelectionForm = {
  groups: "",
  projects: "",
  tags: "",
  template: "",
  drift: [],
  findings: "",
  policy: "",
};

export function selectionFrom(form: SelectionForm): Selection {
  const selection: Selection = {};
  if (list(form.groups).length) selection.groups = list(form.groups);
  if (list(form.projects).length) selection.projects = list(form.projects);
  if (list(form.tags).length) selection.tags = list(form.tags);
  if (form.template.trim()) selection.template = form.template.trim();
  if (form.drift.length) selection.drift = form.drift;
  if (form.findings) selection.findings = form.findings;
  if (form.policy.trim()) selection.policy = form.policy.trim();
  return selection;
}

function formFrom(selection: Selection): SelectionForm {
  return {
    groups: (selection.groups ?? []).join(", "),
    projects: (selection.projects ?? []).join("\n"),
    tags: (selection.tags ?? []).join(", "),
    template: selection.template ?? "",
    drift: selection.drift ?? [],
    findings: selection.findings ?? "",
    policy: selection.policy ?? "",
  };
}

function SelectionFields({
  form,
  onChange,
}: {
  form: SelectionForm;
  onChange: (form: SelectionForm) => void;
}) {
  const id = useId();
  const text = (
    key: "groups" | "projects" | "tags" | "template" | "policy",
    label: string,
    hint: string,
    rows = 1,
  ) => (
    <div>
      <label htmlFor={`${id}-${key}`} className="form-label">
        {label}
      </label>
      <textarea
        id={`${id}-${key}`}
        className="form-control"
        rows={rows}
        value={form[key]}
        aria-describedby={`${id}-${key}-hint`}
        onChange={(event) => {
          onChange({ ...form, [key]: event.target.value });
        }}
      />
      <p id={`${id}-${key}-hint`} className="form-hint">
        {hint}
      </p>
    </div>
  );
  return (
    <fieldset className="stack-1">
      <legend className="fw-medium">Which projects</legend>
      <p className="text-secondary small">
        A project must meet every criterion given. Only active, monitored, unarchived projects of
        this connection are ever chosen.
      </p>
      {text("groups", "Groups", "Full group paths, separated by commas; subgroups are included.")}
      {text("projects", "Projects", "Exact project paths, one per line.", 2)}
      {text("tags", "Tags", "key=value, separated by commas; owner and business unit are tags.")}
      {text("template", "Template in force", "Projects where this template decides a setting.")}
      {text("policy", "Baseline", "With findings: only this baseline's findings count.")}
      <div>
        <span className="form-label">Drift</span>
        <div className="d-flex flex-wrap gap-3">
          {(["drift", "unknown", "aligned"] as const).map((status) => (
            <label key={status} className="form-check">
              <input
                type="checkbox"
                className="form-check-input"
                checked={form.drift.includes(status)}
                onChange={(event) => {
                  onChange({
                    ...form,
                    drift: event.target.checked
                      ? [...form.drift, status]
                      : form.drift.filter((value) => value !== status),
                  });
                }}
              />
              <span className="form-check-label">{status}</span>
            </label>
          ))}
        </div>
      </div>
      <label className="form-label">
        Findings
        <select
          className="form-select mt-1 w-auto"
          value={form.findings}
          onChange={(event) => {
            onChange({ ...form, findings: event.target.value as SelectionForm["findings"] });
          }}
        >
          <option value="">Any</option>
          <option value="open">With open findings</option>
          <option value="none">Without open findings</option>
        </select>
      </label>
    </fieldset>
  );
}

// --- building a change set ---

function BuildForm({
  version,
  onCreated,
}: {
  version: number;
  onCreated: (set: ChangeSet) => void;
}) {
  const id = useId();
  const { token } = usePermissions();
  const saved = useLoad(`saved-selections#${String(version)}`, (signal) =>
    listSavedSelections(signal),
  );
  const [title, setTitle] = useState("");
  const [form, setForm] = useState<SelectionForm>(EMPTY);
  const [kind, setKind] = useState<"template" | "settings">("template");
  const [template, setTemplate] = useState("");
  const [settings, setSettings] = useState("");
  const [hours, setHours] = useState("72");
  const [saveName, setSaveName] = useState("");
  const [preview, setPreview] = useState<SelectionPreview | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const selection = selectionFrom(form);
  const empty = Object.keys(selection).length === 0;

  async function run(action: () => Promise<void>, fallback: string) {
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      await action();
    } catch (error) {
      setFailure(failureText(error, fallback));
    } finally {
      setBusy(false);
    }
  }

  function show() {
    if (!token) return;
    void run(async () => {
      setPreview(await previewSelection(selection, token));
    }, "The selection could not be previewed.");
  }

  function save() {
    if (!token || !saveName.trim()) return;
    void run(async () => {
      await saveSelection(saveName.trim(), selection, token);
      setNote(`Saved the selection as ${saveName.trim()}.`);
      setSaveName("");
      saved.retry();
    }, "The selection could not be saved.");
  }

  function remove(savedId: number, name: string) {
    if (!token) return;
    void run(async () => {
      await deleteSavedSelection(savedId, token);
      setNote(`Deleted the saved selection ${name}.`);
      saved.retry();
    }, "The selection could not be deleted.");
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    let parsed: Record<string, unknown> = {};
    if (kind === "settings") {
      try {
        parsed = JSON.parse(settings) as Record<string, unknown>;
      } catch {
        setFailure("Settings must be JSON in the template settings format.");
        return;
      }
    }
    void run(async () => {
      const created = await createChangeSet(
        {
          title: title.trim(),
          selection,
          ...(kind === "template" ? { template: template.trim() } : { settings: parsed }),
          expires_in_hours: Number(hours),
        },
        token,
      );
      onCreated(created);
    }, "The change set could not be built.");
  }

  return (
    <form onSubmit={submit} className="card card-body stack-2" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="h3">
        Build a change set
      </h2>
      <p className="text-secondary">
        Building reads only stored evidence; nothing changes on the provider. Applying approved sets
        comes in a later release.
      </p>
      <label className="form-label">
        Title
        <input
          type="text"
          className="form-control mt-1"
          maxLength={200}
          value={title}
          onChange={(event) => {
            setTitle(event.target.value);
          }}
        />
      </label>
      {saved.data && saved.data.selections.length > 0 && (
        <div>
          <span className="form-label">Saved selections</span>
          <ul className="list-unstyled stack-1">
            {saved.data.selections.map((row) => (
              <li key={row.id} className="d-flex flex-wrap gap-2 align-items-center">
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => {
                    setForm(formFrom(row.selection));
                    setPreview(null);
                  }}
                >
                  {`Use ${row.name}`}
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost-danger"
                  disabled={busy}
                  onClick={() => {
                    remove(row.id, row.name);
                  }}
                >
                  {`Delete ${row.name}`}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <SelectionFields
        form={form}
        onChange={(next) => {
          setForm(next);
          setPreview(null);
        }}
      />
      <div className="d-flex flex-wrap gap-2 align-items-end">
        <button type="button" className="btn" disabled={busy || empty} onClick={show}>
          Preview projects
        </button>
        <label className="form-label mb-0">
          Save as
          <input
            type="text"
            className="form-control mt-1"
            maxLength={100}
            value={saveName}
            onChange={(event) => {
              setSaveName(event.target.value);
            }}
          />
        </label>
        <button
          type="button"
          className="btn"
          disabled={busy || empty || !saveName.trim()}
          onClick={save}
        >
          Save selection
        </button>
      </div>
      {preview && (
        <div role="region" aria-label="Selected projects">
          <p className="fw-medium">
            {`${String(preview.projects)} projects selected`}
            {preview.truncated && " (first 200 listed)"}
          </p>
          <ul className="small">
            {preview.listed.map((project) => (
              <li key={project.id}>{project.path}</li>
            ))}
          </ul>
        </div>
      )}
      <fieldset className="stack-1">
        <legend className="fw-medium">Wanted values</legend>
        <div className="d-flex gap-3">
          <label className="form-check">
            <input
              type="radio"
              className="form-check-input"
              checked={kind === "template"}
              onChange={() => {
                setKind("template");
              }}
            />
            <span className="form-check-label">A template version</span>
          </label>
          <label className="form-check">
            <input
              type="radio"
              className="form-check-input"
              checked={kind === "settings"}
              onChange={() => {
                setKind("settings");
              }}
            />
            <span className="form-check-label">Settings</span>
          </label>
        </div>
        {kind === "template" ? (
          <label className="form-label">
            Template version
            <input
              type="text"
              className="form-control mt-1"
              placeholder="base-repository@2"
              value={template}
              onChange={(event) => {
                setTemplate(event.target.value);
              }}
            />
          </label>
        ) : (
          <label className="form-label">
            Settings (JSON)
            <textarea
              className="form-control mt-1 font-monospace"
              rows={4}
              placeholder='{"visibility": "private", "merge_requests": {"min_approvals": 2}}'
              value={settings}
              onChange={(event) => {
                setSettings(event.target.value);
              }}
            />
          </label>
        )}
      </fieldset>
      <label className="form-label">
        Approval window in hours (at most 168)
        <input
          type="number"
          min={1}
          max={168}
          className="form-control mt-1 w-auto"
          value={hours}
          onChange={(event) => {
            setHours(event.target.value);
          }}
        />
      </label>
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
      {note && <p role="status">{note}</p>}
      <div>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={
            busy ||
            empty ||
            !title.trim() ||
            (kind === "template" ? !template.trim() : !settings.trim())
          }
        >
          {busy ? "Working…" : "Build change set"}
        </button>
      </div>
    </form>
  );
}

// --- one change set ---

function TargetDetail({ target }: { target: ChangeTarget }) {
  const columns: Column<ChangeTarget["changes"][number]>[] = [
    { key: "setting", header: "Setting", rowHeader: true, render: (row) => settingName(row.key) },
    { key: "before", header: "Now", render: (row) => valueText(row.before) },
    { key: "after", header: "After", render: (row) => valueText(row.after) },
    {
      key: "warnings",
      header: "Warnings",
      render: (row) =>
        row.warnings.length === 0 ? (
          "None"
        ) : (
          <ul className="list-unstyled mb-0">
            {row.warnings.map((warning) => (
              <li key={warning.code}>
                <Badge tone={warning.code === "disrupts_work" ? "warning" : "danger"}>
                  {warningLabels[warning.code]}
                </Badge>{" "}
                {warning.text}
              </li>
            ))}
          </ul>
        ),
    },
  ];
  return (
    <div className="stack-1">
      <h4 className="h4 mb-0">{target.project.path}</h4>
      {target.changes.length > 0 && (
        <ResponsiveTable
          columns={columns}
          rows={target.changes}
          rowKey={(row) => row.key}
          caption={`Changes to ${target.project.path}`}
        />
      )}
      {target.skipped.length > 0 && (
        <ul className="small mb-0">
          {target.skipped.map((skip) => (
            <li key={skip.key}>
              {`${settingName(skip.key)}: skipped, ${skipLabels[skip.code].toLowerCase()}. `}
              <span className="text-secondary">{skip.reason}</span>
            </li>
          ))}
        </ul>
      )}
      {target.unchanged.length > 0 && (
        <p className="small text-secondary mb-0">
          {`Already as wanted: ${target.unchanged.map(settingName).join(", ")}.`}
        </p>
      )}
    </div>
  );
}

function Decisions({ set, onChanged }: { set: ChangeSet; onChanged: (text: string) => void }) {
  const { token, userId, canApprove, canPlan } = usePermissions();
  const [note, setNote] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  if (set.state !== "proposed") return null;
  const own = userId === set.created_by.user_id;

  async function act(action: "approve" | "reject" | "withdraw") {
    if (!token) return;
    setFailure(null);
    try {
      const body =
        action === "approve"
          ? { digest: set.digest, ...(note.trim() ? { note: note.trim() } : {}) }
          : action === "reject"
            ? { note: note.trim() }
            : {};
      const result = await decideChangeSet(set.id, action, body, token);
      const done =
        action === "withdraw"
          ? "Withdrew"
          : action === "reject"
            ? "Rejected"
            : result.state === "approved"
              ? "Approved"
              : "Recorded the first approval of";
      onChanged(`${done} change set ${set.title}.`);
    } catch (error) {
      setFailure(failureText(error, "The change set could not be changed."));
    }
  }

  return (
    <div className="stack-1">
      {canApprove && !own && (
        <>
          <label className="form-label">
            Note
            <input
              type="text"
              className="form-control mt-1"
              maxLength={2000}
              value={note}
              onChange={(event) => {
                setNote(event.target.value);
              }}
            />
          </label>
          <div className="d-flex flex-wrap gap-2">
            <button type="button" className="btn btn-primary" onClick={() => void act("approve")}>
              {`Approve change set ${set.title}`}
            </button>
            <button
              type="button"
              className="btn"
              disabled={!note.trim()}
              onClick={() => void act("reject")}
            >
              Reject (with a note)
            </button>
          </div>
        </>
      )}
      {canPlan && own && (
        <div>
          <button type="button" className="btn" onClick={() => void act("withdraw")}>
            Withdraw
          </button>
        </div>
      )}
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
    </div>
  );
}

function ChangeSetDetail({
  id,
  version,
  onChanged,
}: {
  id: number;
  version: number;
  onChanged: (text: string) => void;
}) {
  const loaded = useLoad(`change-set:${String(id)}#${String(version)}`, (signal) =>
    getChangeSet(id, signal),
  );
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Loading rows={3} />;
  const set = loaded.data;
  const changing = set.document.targets.filter((target) => target.changes.length > 0);
  const quiet = set.document.targets.filter((target) => target.changes.length === 0);
  return (
    <section aria-labelledby="change-set-title" className="card card-body stack-2">
      <h2 id="change-set-title" className="h3">
        {set.title}
      </h2>
      <p>
        <Badge tone={stateTones[set.state]}>{set.state}</Badge>{" "}
        {`${String(set.changes)} changes to ${String(set.targets)} projects, ${String(set.skipped)} skipped. Source: ${set.source ?? "settings"}. Until ${new Date(set.expires_at).toLocaleString()}.`}
      </p>
      {set.sensitive && (
        <Notice tone="warning" title="Sensitive">
          It weakens protection or removes access, so two people other than its creator must approve
          it.
          {set.first_approved_by &&
            ` First approved by ${set.first_approved_by.display_name ?? "someone"}.`}
        </Notice>
      )}
      <DigestDisplay value={set.digest} />
      <Decisions set={set} onChanged={onChanged} />
      {changing.map((target) => (
        <TargetDetail key={target.project.id} target={target} />
      ))}
      {quiet.length > 0 && (
        <details>
          <summary>{`${String(quiet.length)} projects with nothing to change`}</summary>
          {quiet.map((target) => (
            <TargetDetail key={target.project.id} target={target} />
          ))}
        </details>
      )}
    </section>
  );
}

// --- the page ---

function ChangeSetList({ version, onOpen }: { version: number; onOpen: (id: number) => void }) {
  const loaded = useLoad(`change-sets#${String(version)}`, (signal) => listChangeSets(signal));
  const columns: Column<ChangeSetSummary>[] = [
    { key: "title", header: "Change set", rowHeader: true, render: (row) => row.title },
    {
      key: "state",
      header: "State",
      render: (row) => (
        <>
          <Badge tone={stateTones[row.state]}>{row.state}</Badge>
          {row.sensitive && (
            <>
              {" "}
              <Badge tone="warning">sensitive</Badge>
            </>
          )}
          {row.approvals_needed > 0 && (
            <p className="small text-secondary mb-0">{`${String(row.approvals_needed)} approvals needed`}</p>
          )}
        </>
      ),
    },
    {
      key: "size",
      header: "Changes",
      priority: 2,
      render: (row) => `${String(row.changes)} to ${String(row.targets)} projects`,
    },
    {
      key: "open",
      header: "Actions",
      render: (row) => (
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => {
            onOpen(row.id);
          }}
        >
          {`Open ${row.title}`}
        </button>
      ),
    },
  ];
  return (
    <section aria-labelledby="change-sets-title" className="stack-2">
      <h2 id="change-sets-title" className="h3">
        Change sets
      </h2>
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {loaded.loading && <Loading rows={2} />}
      {loaded.data && loaded.data.change_sets.length === 0 && (
        <EmptyState title="No change sets yet" />
      )}
      {loaded.data && loaded.data.change_sets.length > 0 && (
        <ResponsiveTable
          columns={columns}
          rows={loaded.data.change_sets}
          rowKey={(row) => row.id}
          caption="Change sets"
        />
      )}
    </section>
  );
}

export function ChangeSetsPage() {
  const { canPlan } = usePermissions();
  const [version, setVersion] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const status = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (message) status.current?.focus();
  }, [message]);

  function changed(text: string) {
    setMessage(text);
    setVersion((value) => value + 1);
  }

  return (
    <section aria-labelledby="page-title" className="stack-4">
      <GovernanceHeader intro="A change set lists the exact template changes to chosen projects, with today's values from evidence, and is approved before anything is applied." />
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="fw-medium">
          {message}
        </p>
      )}
      <ChangeSetList version={version} onOpen={setOpen} />
      {open !== null && <ChangeSetDetail id={open} version={version} onChanged={changed} />}
      {canPlan && (
        <BuildForm
          version={version}
          onCreated={(set) => {
            changed(`Built change set ${set.title}: ${String(set.changes)} changes.`);
            setOpen(set.id);
          }}
        />
      )}
    </section>
  );
}
