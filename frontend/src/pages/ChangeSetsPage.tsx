import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import {
  applyChangeSet,
  continueChangeSet,
  createChangeSet,
  decideChangeSet,
  deleteSavedSelection,
  getChangeSet,
  listChangeSets,
  listSavedSelections,
  previewSelection,
  runChangeSet,
  saveSelection,
  type ChangeItem,
  type ChangeSet,
  type ChangeSetState,
  type ChangeSetSummary,
  type ChangeTarget,
  type DriftFilter,
  type ItemStatus,
  type Selection,
  type SelectionPreview,
  type SkipCode,
  type Verification,
  type VerificationStatus,
  type WarningCode,
  type WriteRefusal,
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
  applying: "info",
  paused: "warning",
  cancelled: "secondary",
  finished: "success",
  rolling_back: "info",
  rolled_back: "secondary",
};

export const itemLabels: Record<ItemStatus, string> = {
  pending: "Waiting",
  writing: "Writing",
  applied: "Applied",
  skipped: "Already as wanted",
  out_of_date: "Changed since built",
  manual: "Change by hand",
  failed: "Failed",
  cancelled: "Not applied",
};

const itemTones: Record<ItemStatus, Tone> = {
  pending: "secondary",
  writing: "info",
  applied: "success",
  skipped: "secondary",
  out_of_date: "warning",
  manual: "warning",
  failed: "danger",
  cancelled: "secondary",
};

export const refusalLabels: Record<WriteRefusal, string> = {
  writes_disabled: "Template writes are off for this deployment (BALUARTE_TEMPLATE_WRITES).",
  token_missing: "No template write token is stored (Settings).",
  not_primary: "Change sets are applied only to the primary GitLab connection.",
  busy: "Another change set of this connection is applying or rolling back.",
};

const RUNNING: ChangeSetState[] = ["applying", "rolling_back"];

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
    canPlan: permissions.includes("changes:plan"),
    canApprove: permissions.includes("changes:approve"),
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
  const [kind, setKind] = useState<"template" | "settings" | "effective">("template");
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
          ...(kind === "template"
            ? { template: template.trim() }
            : kind === "settings"
              ? { settings: parsed }
              : { effective: true }),
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
          <label className="form-check">
            <input
              type="radio"
              className="form-check-input"
              checked={kind === "effective"}
              onChange={() => {
                setKind("effective");
              }}
            />
            <span className="form-check-label">Each project&apos;s templates (fix drift)</span>
          </label>
        </div>
        {kind === "effective" ? (
          <p className="small text-secondary mb-0">
            Each project gets the settings its own templates and overrides put in force, so drift is
            put right without choosing one template for all.
          </p>
        ) : kind === "template" ? (
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
            (kind === "template" && !template.trim()) ||
            (kind === "settings" && !settings.trim())
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
    ...(target.changes.some((change) => change.decided_by)
      ? [
          {
            key: "decided",
            header: "Decided by",
            priority: 3 as const,
            render: (row: ChangeTarget["changes"][number]) =>
              row.decided_by
                ? `${row.decided_by.template}${row.decided_by.origin === "override" ? " (override)" : ""} on ${row.decided_by.assigned_to}`
                : "",
          },
        ]
      : []),
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
  const own = set.created_by !== null && userId === set.created_by.user_id;

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

function Progress({ items }: { items: ChangeItem[] }) {
  if (items.length === 0) return null;
  const columns: Column<ChangeItem>[] = [
    { key: "project", header: "Project", render: (item) => item.project_path, rowHeader: true },
    { key: "setting", header: "Setting", render: (item) => settingName(item.setting) },
    {
      key: "change",
      header: "Change",
      render: (item) => `${valueText(item.before)} → ${valueText(item.after)}`,
    },
    {
      key: "status",
      header: "Status",
      render: (item) => (
        <>
          <Badge tone={itemTones[item.status]}>{itemLabels[item.status]}</Badge>
          {item.rollback_status && (
            <>
              {" "}
              <Badge tone={item.rollback_status === "rolled_back" ? "secondary" : "warning"}>
                {item.rollback_status === "rolled_back"
                  ? "Rolled back"
                  : item.rollback_status === "failed"
                    ? "Rollback failed"
                    : "Not rolled back"}
              </Badge>
            </>
          )}
        </>
      ),
    },
    {
      key: "message",
      header: "Details",
      priority: 2,
      render: (item) => [item.message, item.rollback_message].filter(Boolean).join(" "),
    },
  ];
  return (
    <div>
      <h3 className="h4">Progress</h3>
      <ResponsiveTable
        caption="Each change and what happened to it"
        columns={columns}
        rows={items}
        rowKey={(item) => item.id}
      />
    </div>
  );
}

export const verificationLabels: Record<VerificationStatus, string> = {
  verified: "Verified",
  drift: "Changed again",
  awaiting_collection: "Awaiting collection",
  unknown: "Cannot tell",
};

const verificationTones: Record<VerificationStatus, Tone> = {
  verified: "success",
  drift: "danger",
  awaiting_collection: "secondary",
  unknown: "warning",
};

export function sourceText(set: ChangeSetSummary): string {
  if (set.source_kind === "effective") return "each project's templates";
  return set.source ?? "settings";
}

function EvidenceCheck({ checks }: { checks: Verification[] }) {
  if (checks.length === 0) return null;
  const columns: Column<Verification>[] = [
    { key: "project", header: "Project", render: (row) => row.project_path, rowHeader: true },
    { key: "setting", header: "Setting", render: (row) => settingName(row.setting) },
    { key: "after", header: "Written", render: (row) => valueText(row.after) },
    {
      key: "status",
      header: "Evidence",
      render: (row) => (
        <>
          <Badge tone={verificationTones[row.status]}>{verificationLabels[row.status]}</Badge>
          {row.status === "drift" && ` Now ${valueText(row.observed)}.`}
        </>
      ),
    },
    {
      key: "reason",
      header: "Details",
      priority: 2,
      render: (row) =>
        row.observed_at
          ? `${row.reason} Collected ${new Date(row.observed_at).toLocaleString()}.`
          : row.reason,
    },
  ];
  return (
    <div>
      <h3 className="h4">Evidence check</h3>
      <p className="text-secondary">
        Each applied change compared with what discovery collected after the write. Discovery runs
        after every stage.
      </p>
      <ResponsiveTable
        caption="Applied changes against collected evidence"
        columns={columns}
        rows={checks}
        rowKey={(row) => row.item_id}
      />
    </div>
  );
}

function Execution({ set, onChanged }: { set: ChangeSet; onChanged: (text: string) => void }) {
  const { token, canApprove } = usePermissions();
  const [stage, setStage] = useState(String(Math.min(5, Math.max(1, set.changing_targets))));
  const [more, setMore] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const stageId = useId();
  const moreId = useId();
  const startable = set.state === "approved";
  const executing = !["proposed", "rejected", "withdrawn", "expired"].includes(set.state);
  if (!canApprove || (!startable && !executing)) return null;
  const blocked = set.write_refusals.length > 0;

  async function act(run: (csrf: string) => Promise<ChangeSet>, done: string) {
    if (!token) return;
    setFailure(null);
    try {
      await run(token);
      onChanged(done);
    } catch (error) {
      setFailure(failureText(error, "The change set could not be changed."));
    }
  }

  const stageCount = Number.parseInt(stage, 10);
  const moreCount = more.trim() ? Number.parseInt(more, 10) : null;
  return (
    <section aria-labelledby={`${stageId}-heading`} className="stack-1">
      <h3 id={`${stageId}-heading`} className="h4">
        Applying
      </h3>
      {blocked && (
        <Notice tone="info" title="Cannot write now">
          <ul className="mb-0">
            {set.write_refusals.map((code) => (
              <li key={code}>{refusalLabels[code]}</li>
            ))}
          </ul>
        </Notice>
      )}
      {set.halt_reason && (
        <Notice tone="warning" title="The last run stopped">
          {set.halt_reason}
        </Notice>
      )}
      {executing && (
        <p>
          {`${String(set.stage_targets)} of ${String(set.changing_targets)} projects included so far.`}
          {set.applied_by && ` Started by ${set.applied_by.display_name ?? "someone"}.`}
          {set.cancelled_by && ` Cancelled by ${set.cancelled_by.display_name ?? "someone"}.`}
          {set.rollback_by && ` Rollback started by ${set.rollback_by.display_name ?? "someone"}.`}
        </p>
      )}
      {startable && (
        <>
          {set.irreversible.length > 0 && (
            <Notice tone="warning" title="Cannot be fully undone">
              <ul>
                {set.irreversible.map((found) => (
                  <li key={`${found.project}:${found.setting}`}>
                    {`${found.project}, ${settingName(found.setting)}: ${found.why}`}
                  </li>
                ))}
              </ul>
              <label className="form-check">
                <input
                  type="checkbox"
                  className="form-check-input"
                  checked={acknowledged}
                  onChange={(event) => {
                    setAcknowledged(event.target.checked);
                  }}
                />
                <span className="form-check-label">I understand these cannot be fully undone</span>
              </label>
            </Notice>
          )}
          <div className="d-flex flex-wrap gap-2 align-items-end">
            <label className="form-label mb-0" htmlFor={stageId}>
              Projects in the first stage
              <input
                id={stageId}
                type="number"
                min={1}
                max={set.changing_targets}
                className="form-control mt-1"
                value={stage}
                onChange={(event) => {
                  setStage(event.target.value);
                }}
              />
            </label>
            <button
              type="button"
              className="btn btn-primary"
              disabled={
                blocked || !(stageCount >= 1) || (set.irreversible.length > 0 && !acknowledged)
              }
              onClick={() =>
                void act(
                  (csrf) => applyChangeSet(set.id, stageCount, acknowledged, csrf),
                  `Started applying change set ${set.title}.`,
                )
              }
            >
              {`Apply change set ${set.title}`}
            </button>
          </div>
        </>
      )}
      {set.state === "paused" && (
        <div className="d-flex flex-wrap gap-2 align-items-end">
          <label className="form-label mb-0" htmlFor={moreId}>
            More projects (empty for all that remain)
            <input
              id={moreId}
              type="number"
              min={1}
              className="form-control mt-1"
              value={more}
              onChange={(event) => {
                setMore(event.target.value);
              }}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            disabled={blocked || (moreCount !== null && !(moreCount >= 1))}
            onClick={() =>
              void act(
                (csrf) => continueChangeSet(set.id, moreCount, csrf),
                `Continued change set ${set.title}.`,
              )
            }
          >
            Continue
          </button>
        </div>
      )}
      <div className="d-flex flex-wrap gap-2">
        {(set.state === "applying" || set.state === "paused") && (
          <button
            type="button"
            className="btn"
            onClick={() =>
              void act(
                (csrf) => runChangeSet(set.id, "cancel", csrf),
                `Cancelled change set ${set.title}.`,
              )
            }
          >
            Cancel
          </button>
        )}
        {(["paused", "cancelled", "finished"].includes(set.state) ||
          (set.state === "rolling_back" && set.halt_reason !== null)) && (
          <button
            type="button"
            className="btn btn-danger"
            disabled={blocked}
            onClick={() =>
              void act(
                (csrf) => runChangeSet(set.id, "rollback", csrf),
                `Started rolling back change set ${set.title}.`,
              )
            }
          >
            Roll back
          </button>
        )}
      </div>
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
    </section>
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
  const [tick, setTick] = useState(0);
  const loaded = useLoad(`change-set:${String(id)}#${String(version)}.${String(tick)}`, (signal) =>
    getChangeSet(id, signal),
  );
  const running = loaded.data ? RUNNING.includes(loaded.data.state) : false;
  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => {
      setTick((value) => value + 1);
    }, 5000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [running, tick]);
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
        {`${String(set.changes)} changes to ${String(set.targets)} projects, ${String(set.skipped)} skipped. Source: ${sourceText(set)}.${set.created_by_system ? " Proposed automatically by Baluarte for drift; it applies nothing until people approve and apply it." : ""} Until ${new Date(set.expires_at).toLocaleString()}.`}
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
      <Execution set={set} onChanged={onChanged} />
      <Progress items={set.items} />
      <EvidenceCheck checks={set.verification} />
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
  const [params] = useSearchParams();
  const [version, setVersion] = useState(0);
  const [open, setOpen] = useState<number | null>(() => {
    const asked = Number(params.get("open"));
    return Number.isInteger(asked) && asked > 0 ? asked : null;
  });
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
