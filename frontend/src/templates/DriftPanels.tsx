import { useId, useState, type FormEvent } from "react";
import { useNavigate } from "react-router";

import { createChangeSet, type Selection } from "../api/changeSets";
import {
  decideTemplateException,
  getAlignment,
  getProjectDrift,
  listTemplateExceptions,
  requestTemplateException,
  type DriftStatus,
  type GroupTally,
  type ProjectRef,
  type SettingDrift,
  type TemplateException,
} from "../api/templates";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { Badge } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { KpiCard } from "../ui/KpiCard";
import { Loading } from "../ui/Loading";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";
import {
  driftLabels,
  driftTones,
  failureText,
  settingLabels,
  sourceText,
  useCsrf,
  valueText,
} from "./format";

const DEFAULT_DAYS = 30;

/** Builds a change set from each chosen project's own templates and opens it for review. */
export function FixDriftButton({
  title,
  selection,
  label,
}: {
  title: string;
  selection: Selection;
  label: string;
}) {
  const { token, canPlan } = useCsrf();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  if (!canPlan) return null;

  async function propose() {
    if (!token) return;
    setBusy(true);
    setFailure(null);
    try {
      const created = await createChangeSet(
        { title, selection, effective: true, expires_in_hours: 72 },
        token,
      );
      void navigate(`/governance/change-sets?open=${String(created.id)}`);
    } catch (error) {
      setFailure(failureText(error, "The change set could not be built."));
      setBusy(false);
    }
  }

  return (
    <div className="stack-1">
      <div>
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy}
          onClick={() => {
            void propose();
          }}
        >
          {busy ? "Building…" : label}
        </button>
      </div>
      <p className="small text-secondary mb-0">
        Builds a change set to review and approve; nothing changes until it is applied.
      </p>
      {failure && (
        <p role="alert" className="text-danger-emphasis mb-0">
          {failure}
        </p>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: DriftStatus }) {
  return <Badge tone={driftTones[status]}>{driftLabels[status]}</Badge>;
}

function countsText(counts: Partial<Record<DriftStatus, number>>): string {
  return (["drift", "unknown", "excepted", "aligned", "unsupported"] as const)
    .filter((status) => counts[status])
    .map((status) => `${String(counts[status])} ${driftLabels[status].toLowerCase()}`)
    .join(", ");
}

function dateText(value: string): string {
  return new Date(value).toLocaleDateString();
}

function personText(person: { user_id: number; display_name: string | null } | null): string {
  if (!person) return "no one";
  return person.display_name ?? `user ${String(person.user_id)}`;
}

// --- alignment ---

function TallyTable({ caption, rows }: { caption: string; rows: GroupTally[] }) {
  const columns: Column<GroupTally>[] = [
    { key: "name", header: caption, rowHeader: true, render: (row) => row.name },
    { key: "projects", header: "Projects", numeric: true, render: (row) => row.projects },
    {
      key: "drifting",
      header: "Drifting",
      numeric: true,
      render: (row) => row.projects_drifting,
    },
    {
      key: "settings",
      header: "Settings",
      priority: 2,
      render: (row) => countsText(row.settings),
    },
  ];
  return (
    <ResponsiveTable
      columns={columns}
      rows={rows}
      rowKey={(row) => row.name}
      caption={`Alignment by ${caption.toLowerCase()}`}
      showCaption
    />
  );
}

/** How the connection's projects compare with their templates, and which need attention. */
export function AlignmentPanel({
  version,
  onShow,
}: {
  version: number;
  onShow: (project: ProjectRef) => void;
}) {
  const loaded = useLoad(`template-alignment#${String(version)}`, (signal) => getAlignment(signal));
  return (
    <section aria-labelledby="alignment-title" className="stack-2">
      <h2 id="alignment-title" className="h3">
        Alignment
      </h2>
      <p className="text-secondary">
        Each setting in force is compared with what was last collected. Unknown means the evidence
        cannot tell, never that the setting is right.
      </p>
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {loaded.loading && <Loading rows={3} />}
      {loaded.data && loaded.data.projects_with_templates === 0 && (
        <EmptyState title="No template applies to any project yet" />
      )}
      {loaded.data && loaded.data.projects_with_templates > 0 && (
        <>
          <div className="row row-cards">
            <div className="col-6 col-lg-3">
              <KpiCard
                label="Projects with templates"
                value={loaded.data.projects_with_templates}
                footnote={`of ${String(loaded.data.projects)} monitored`}
              />
            </div>
            <div className="col-6 col-lg-3">
              <KpiCard label="Aligned" value={loaded.data.projects_aligned} />
            </div>
            <div className="col-6 col-lg-3">
              <KpiCard label="Drifting" value={loaded.data.projects_drifting} />
            </div>
            <div className="col-6 col-lg-3">
              <KpiCard
                label="Cannot tell"
                value={loaded.data.projects_unknown}
                footnote={
                  loaded.data.expiring_exceptions > 0
                    ? `${String(loaded.data.expiring_exceptions)} exceptions end within 14 days`
                    : undefined
                }
              />
            </div>
          </div>
          {loaded.data.projects_drifting > 0 && (
            <FixDriftButton
              title="Fix template drift"
              selection={{ drift: ["drift"] }}
              label="Propose a fix for drifting projects"
            />
          )}
          {loaded.data.attention.length > 0 && (
            <ResponsiveTable
              columns={[
                {
                  key: "project",
                  header: "Project",
                  rowHeader: true,
                  render: (row) => row.project.path,
                },
                {
                  key: "status",
                  header: "Status",
                  render: (row) => <StatusBadge status={row.status} />,
                },
                {
                  key: "counts",
                  header: "Settings",
                  priority: 2,
                  render: (row) => countsText(row.counts),
                },
                {
                  key: "show",
                  header: "Actions",
                  render: (row) => (
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => {
                        onShow(row.project);
                      }}
                    >
                      {`Show ${row.project.path}`}
                    </button>
                  ),
                },
              ]}
              rows={loaded.data.attention}
              rowKey={(row) => row.project.id}
              caption={
                loaded.data.attention_truncated
                  ? "Projects needing attention (first 200)"
                  : "Projects needing attention"
              }
              showCaption
            />
          )}
          <TallyTable caption="Template" rows={loaded.data.by_template} />
          <TallyTable caption="Group" rows={loaded.data.by_group} />
        </>
      )}
    </section>
  );
}

// --- one project's drift ---

function RequestForm({
  projectId,
  setting,
  onDone,
}: {
  projectId: number;
  setting: SettingDrift;
  onDone: (text: string) => void;
}) {
  const id = useId();
  const { token } = useCsrf();
  const [justification, setJustification] = useState("");
  const [days, setDays] = useState(String(DEFAULT_DAYS));
  const [failure, setFailure] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const label = settingLabels[setting.key] ?? setting.key;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    setSending(true);
    setFailure(null);
    try {
      const expires = new Date(Date.now() + Number(days) * 24 * 60 * 60 * 1000);
      await requestTemplateException(
        {
          project_id: projectId,
          setting: setting.key,
          justification: justification.trim(),
          expires_at: expires.toISOString(),
        },
        token,
      );
      onDone(`Requested an exception for ${label}. Someone else must approve it.`);
    } catch (error) {
      setFailure(failureText(error, "The exception could not be requested."));
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      className="stack-1 mt-2"
      aria-label={`Request an exception for ${label}`}
    >
      <label htmlFor={`${id}-why`} className="fw-medium">
        Why this project may differ
      </label>
      <textarea
        id={`${id}-why`}
        className="form-control"
        rows={2}
        minLength={10}
        maxLength={4000}
        value={justification}
        onChange={(event) => {
          setJustification(event.target.value);
        }}
      />
      <label className="fw-medium">
        For how many days
        <input
          type="number"
          min={1}
          max={365}
          className="form-control mt-1 w-auto"
          value={days}
          onChange={(event) => {
            setDays(event.target.value);
          }}
        />
      </label>
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
      <div>
        <button
          type="submit"
          className="btn btn-sm btn-primary"
          disabled={sending || justification.trim().length < 10 || !(Number(days) >= 1)}
        >
          {sending ? "Requesting…" : "Request exception"}
        </button>
      </div>
    </form>
  );
}

function EvidenceCell({
  row,
  projectId,
  onChanged,
}: {
  row: SettingDrift;
  projectId: number;
  onChanged: (text: string) => void;
}) {
  const { canRequest } = useCsrf();
  const [asking, setAsking] = useState(false);
  return (
    <>
      <span>{row.reason}</span>
      {row.exception && (
        <p className="small mb-0">
          {`Exception ${row.exception.state} until ${dateText(row.exception.expires_at)}`}
          {row.exception.expiring && <span className="text-warning-emphasis"> (ends soon)</span>}
        </p>
      )}
      {row.status === "drift" && !row.exception && canRequest && !asking && (
        <button
          type="button"
          className="btn btn-sm mt-1"
          onClick={() => {
            setAsking(true);
          }}
        >
          {`Request an exception for ${settingLabels[row.key] ?? row.key}`}
        </button>
      )}
      {asking && <RequestForm projectId={projectId} setting={row} onDone={onChanged} />}
    </>
  );
}

/** Each effective setting of one project against its evidence. */
export function DriftTable({
  projectId,
  version,
  onChanged,
}: {
  projectId: number;
  version: number;
  onChanged: (text: string) => void;
}) {
  const loaded = useLoad(`template-drift:${String(projectId)}#${String(version)}`, (signal) =>
    getProjectDrift(projectId, signal),
  );
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Loading rows={2} />;
  const drift = loaded.data;
  if (drift.settings.length === 0) return null;
  const columns: Column<SettingDrift>[] = [
    {
      key: "setting",
      header: "Setting",
      rowHeader: true,
      render: (row) => settingLabels[row.key] ?? row.key,
    },
    { key: "expected", header: "Template", render: (row) => valueText(row.expected) },
    { key: "status", header: "Status", render: (row) => <StatusBadge status={row.status} /> },
    {
      key: "evidence",
      header: "Evidence",
      render: (row) => <EvidenceCell row={row} projectId={projectId} onChanged={onChanged} />,
    },
    {
      key: "source",
      header: "Decided by",
      priority: 3,
      render: (row) => sourceText(row.decided_by),
    },
  ];
  return (
    <div className="stack-2">
      <ResponsiveTable
        columns={columns}
        rows={drift.settings}
        rowKey={(row) => row.key}
        caption={`Drift of ${drift.project.path}`}
        showCaption
      />
      {drift.settings.some((row) => row.status === "drift") && (
        <FixDriftButton
          title={`Fix template drift in ${drift.project.path}`}
          selection={{ projects: [drift.project.path] }}
          label={`Propose a fix for ${drift.project.path}`}
        />
      )}
    </div>
  );
}

// --- exceptions ---

function Decide({ row, onChanged }: { row: TemplateException; onChanged: (text: string) => void }) {
  const { token, canApprove } = useCsrf();
  const [note, setNote] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const label = `${settingLabels[row.setting] ?? row.setting} on ${row.project_path}`;
  if (!canApprove || !["requested", "approved"].includes(row.state)) return null;

  async function act(action: "approve" | "reject" | "revoke") {
    if (!token) return;
    setFailure(null);
    try {
      await decideTemplateException(row.id, action, note.trim() || null, token);
      const done = { approve: "Approved", reject: "Rejected", revoke: "Revoked" }[action];
      onChanged(`${done} the exception for ${label}.`);
    } catch (error) {
      setFailure(failureText(error, "The exception could not be changed."));
    }
  }

  const needsNote = note.trim().length === 0;
  return (
    <div className="stack-1">
      <label className="small">
        Note
        <input
          type="text"
          className="form-control form-control-sm mt-1"
          maxLength={2000}
          value={note}
          onChange={(event) => {
            setNote(event.target.value);
          }}
        />
      </label>
      <div className="d-flex flex-wrap gap-1">
        {row.state === "requested" && (
          <>
            <button type="button" className="btn btn-sm" onClick={() => void act("approve")}>
              {`Approve exception for ${label}`}
            </button>
            <button
              type="button"
              className="btn btn-sm"
              disabled={needsNote}
              onClick={() => void act("reject")}
            >
              Reject (with a note)
            </button>
          </>
        )}
        <button
          type="button"
          className="btn btn-sm"
          disabled={needsNote}
          onClick={() => void act("revoke")}
        >
          Revoke (with a note)
        </button>
      </div>
      {failure && (
        <p role="alert" className="text-danger-emphasis small">
          {failure}
        </p>
      )}
    </div>
  );
}

/** Template exceptions: waivers of one setting for one project, approved by someone else. */
export function ExceptionsPanel({
  version,
  onChanged,
}: {
  version: number;
  onChanged: (text: string) => void;
}) {
  const loaded = useLoad(`template-exceptions#${String(version)}`, (signal) =>
    listTemplateExceptions(signal),
  );
  const columns: Column<TemplateException>[] = [
    {
      key: "what",
      header: "Setting",
      rowHeader: true,
      render: (row) => (
        <>
          <span>{settingLabels[row.setting] ?? row.setting}</span>
          <p className="small text-secondary mb-0">{row.project_path}</p>
        </>
      ),
    },
    {
      key: "state",
      header: "State",
      render: (row) => (
        <>
          <span>{row.state}</span>
          <p className="small text-secondary mb-0">
            {`until ${dateText(row.expires_at)}`}
            {row.expiring && " (ends soon)"}
          </p>
        </>
      ),
    },
    {
      key: "why",
      header: "Justification",
      priority: 2,
      render: (row) => (
        <>
          <span>{row.justification}</span>
          <p className="small text-secondary mb-0">
            {`Requested by ${personText(row.requested_by)}`}
            {row.decided_by &&
              `; ${row.state === "rejected" ? "rejected" : "approved"} by ${personText(row.decided_by)}`}
          </p>
        </>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      render: (row) => <Decide row={row} onChanged={onChanged} />,
    },
  ];
  return (
    <section aria-labelledby="template-exceptions-title" className="stack-2">
      <h2 id="template-exceptions-title" className="h3">
        Template exceptions
      </h2>
      <p className="text-secondary">
        An approved exception lets one project differ from one setting until it ends. It waives
        drift only; it never stands in for evidence that is missing.
      </p>
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {loaded.loading && <Loading rows={2} />}
      {loaded.data && loaded.data.exceptions.length === 0 && (
        <EmptyState title="No template exceptions" />
      )}
      {loaded.data && loaded.data.exceptions.length > 0 && (
        <ResponsiveTable
          columns={columns}
          rows={loaded.data.exceptions}
          rowKey={(row) => row.id}
          caption="Template exceptions"
        />
      )}
    </section>
  );
}
