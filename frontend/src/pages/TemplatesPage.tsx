import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { listProjects, type Project } from "../api/gitlab";
import type { PolicyError, TargetType } from "../api/policies";
import {
  assignTemplate,
  getEffectiveSettings,
  getTemplateVersion,
  listSettingSupport,
  listTemplateAssignments,
  listTemplates,
  saveTemplate,
  unassignTemplate,
  type EffectiveSettings,
  type ProjectRef,
  type Support,
  type TemplateAssignment,
  type TemplateSummary,
} from "../api/templates";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { AlignmentPanel, DriftTable, ExceptionsPanel } from "../templates/DriftPanels";
import {
  failureText,
  placeText,
  settingLabels,
  sourceText,
  supportLabels,
  useCsrf,
  valueText,
} from "../templates/format";
import { TargetPicker, type Picked } from "../policies/TargetPicker";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

export const EXAMPLE_TEMPLATE = `apiVersion: baluarte.io/v1alpha1
kind: ConfigurationTemplate
metadata:
  name: service-repository
  description: Services that ship to production
spec:
  settings:
    visibility: private
    default_branch_protected: true
    merge_requests:
      min_approvals: 2
      forbid_author_approval: true
`;

const MIN_REASON = 20;

// --- templates and versions ---

function VersionDetail({ name, version }: { name: string; version: number }) {
  const loaded = useLoad(`template:${name}@${String(version)}`, (signal) =>
    getTemplateVersion(name, version, signal),
  );
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Loading rows={2} />;
  const detail = loaded.data;
  return (
    <div className="stack-2">
      {detail.chain.length > 0 && <p>{`Extends ${detail.chain.join(", which extends ")}.`}</p>}
      <dl className="row mb-0">
        {Object.entries(detail.resolved_settings).map(([key, value]) => (
          <div key={key} className="col-12 d-flex gap-2">
            <dt className="fw-medium">{settingLabels[key] ?? key}</dt>
            <dd className="mb-0">
              {valueText(value)}
              {!(key in detail.settings) && <span className="text-secondary"> (inherited)</span>}
            </dd>
          </div>
        ))}
      </dl>
      <details>
        <summary>YAML</summary>
        <pre className="mt-2 p-2 border rounded">
          <code>{detail.source_yaml}</code>
        </pre>
      </details>
    </div>
  );
}

function TemplateCard({ template }: { template: TemplateSummary }) {
  const id = useId();
  const [shown, setShown] = useState(template.versions[0]?.version ?? 1);
  const current = template.versions.find((v) => v.version === shown) ?? template.versions[0];
  return (
    <article className="card" aria-labelledby={`${id}-name`}>
      <div className="card-body stack-2">
        <div className="d-flex flex-wrap align-items-center gap-2">
          <h3 id={`${id}-name`} className="card-title mb-0">
            {template.name}
          </h3>
          <label className="ms-auto d-flex align-items-center gap-2">
            Version
            <select
              className="form-select form-select-sm w-auto"
              value={shown}
              onChange={(event) => {
                setShown(Number(event.target.value));
              }}
            >
              {template.versions.map((v) => (
                <option key={v.version} value={v.version}>
                  {`v${String(v.version)}${v === template.versions[0] ? " (latest)" : ""}`}
                </option>
              ))}
            </select>
          </label>
        </div>
        {template.description && <p className="text-secondary mb-0">{template.description}</p>}
        {current && (
          <p className="small text-secondary mb-0">
            {`Saved ${new Date(current.created_at).toLocaleString()} by ${current.created_by}`}
          </p>
        )}
        <VersionDetail name={template.name} version={shown} />
      </div>
    </article>
  );
}

function SaveForm({ onSaved }: { onSaved: (text: string) => void }) {
  const id = useId();
  const { token } = useCsrf();
  const [source, setSource] = useState(EXAMPLE_TEMPLATE);
  const [errors, setErrors] = useState<PolicyError[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    setSaving(true);
    setErrors([]);
    setFailure(null);
    try {
      const result = await saveTemplate(source, token);
      if ("errors" in result) {
        setErrors(result.errors);
      } else {
        const label = `${result.version.name} v${String(result.version.version)}`;
        onSaved(result.created ? `Saved ${label}.` : `No change: ${label} is identical.`);
      }
    } catch (error) {
      setFailure(failureText(error, "The template could not be saved."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      className="stack-2"
      aria-labelledby={`${id}-title`}
    >
      <h2 id={`${id}-title`} className="h3">
        Save a template version
      </h2>
      <p className="text-secondary">
        Saving changed text under a name adds its next version; saved versions never change.{" "}
        <code>extends</code> names one exact version, such as <code>base-repository@2</code>.
      </p>
      <label htmlFor={`${id}-source`} className="fw-medium">
        Template YAML
      </label>
      <textarea
        id={`${id}-source`}
        className="form-control font-monospace"
        rows={14}
        value={source}
        spellCheck={false}
        onChange={(event) => {
          setSource(event.target.value);
        }}
        aria-describedby={errors.length > 0 ? `${id}-errors` : undefined}
      />
      {errors.length > 0 && (
        <div id={`${id}-errors`} role="alert" className="alert alert-danger">
          <p className="mb-1 text-body fw-medium">The template is not valid:</p>
          <ul className="mb-0 text-body">
            {errors.map((error) => (
              <li key={`${error.path}:${error.message}`}>
                <code>{error.path}</code>
                {`: ${error.message}`}
              </li>
            ))}
          </ul>
        </div>
      )}
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
      <div>
        <button type="submit" className="btn btn-primary" disabled={saving || !token}>
          {saving ? "Saving…" : "Save version"}
        </button>
      </div>
    </form>
  );
}

// --- assignments ---

interface OverrideForm {
  visibility: string;
  defaultBranchProtected: string;
  minApprovals: string;
}

const NO_OVERRIDES: OverrideForm = { visibility: "", defaultBranchProtected: "", minApprovals: "" };

export function overridesFrom(form: OverrideForm): Record<string, unknown> | undefined {
  const overrides: Record<string, unknown> = {};
  if (form.visibility) overrides.visibility = form.visibility;
  if (form.defaultBranchProtected) {
    overrides.default_branch_protected = form.defaultBranchProtected === "yes";
  }
  if (form.minApprovals !== "") {
    overrides.merge_requests = { min_approvals: Number(form.minApprovals) };
  }
  return Object.keys(overrides).length > 0 ? overrides : undefined;
}

function AssignForm({
  templates,
  onAssigned,
}: {
  templates: TemplateSummary[];
  onAssigned: (text: string) => void;
}) {
  const id = useId();
  const { token } = useCsrf();
  const [choice, setChoice] = useState(() => {
    const first = templates[0];
    return first ? `${first.name}@${String(first.versions[0]?.version ?? 1)}` : "";
  });
  const [targetType, setTargetType] = useState<TargetType>("instance");
  const [picked, setPicked] = useState<Picked | null>(null);
  const [form, setForm] = useState<OverrideForm>(NO_OVERRIDES);
  const [reason, setReason] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const overrides = overridesFrom(form);
  const needsTarget = targetType !== "instance" && picked?.type !== targetType;
  const reasonShort = overrides !== undefined && reason.trim().length < MIN_REASON;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token || !choice || needsTarget || reasonShort) return;
    const [name, version] = choice.split("@");
    setSaving(true);
    setFailure(null);
    try {
      const assigned = await assignTemplate(
        {
          name: name ?? "",
          version: Number(version),
          target_type: targetType,
          ...(targetType === "instance" || !picked ? {} : { target_id: picked.gitlabId }),
          ...(overrides ? { overrides, override_reason: reason.trim() } : {}),
        },
        token,
      );
      onAssigned(
        `Assigned ${assigned.name} v${String(assigned.version)} to ${placeText(assigned.target).toLowerCase()}.`,
      );
      setForm(NO_OVERRIDES);
      setReason("");
    } catch (error) {
      setFailure(failureText(error, "The template could not be assigned."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="card" aria-labelledby={`${id}-title`}>
      <div className="card-body stack-2">
        <h3 id={`${id}-title`} className="card-title">
          Assign a template
        </h3>
        <p className="text-secondary mb-0">
          A connection, group or project holds one template; assigning another replaces it.
        </p>
        <div className="d-flex flex-wrap gap-3">
          <label className="fw-medium">
            Template version
            <select
              className="form-select mt-1"
              value={choice}
              onChange={(event) => {
                setChoice(event.target.value);
              }}
            >
              {templates.flatMap((template) =>
                template.versions.map((v) => {
                  const value = `${template.name}@${String(v.version)}`;
                  return (
                    <option key={value} value={value}>
                      {`${template.name} v${String(v.version)}`}
                    </option>
                  );
                }),
              )}
            </select>
          </label>
          <label className="fw-medium">
            Applies to
            <select
              className="form-select mt-1"
              value={targetType}
              onChange={(event) => {
                setTargetType(event.target.value as TargetType);
              }}
            >
              <option value="instance">Whole connection</option>
              <option value="group">A group</option>
              <option value="project">A project</option>
            </select>
          </label>
        </div>
        {targetType !== "instance" && (
          <TargetPicker type={targetType} value={picked} onChange={setPicked} />
        )}
        <fieldset className="border rounded p-2 stack-2">
          <legend className="px-1 fw-medium fs-5">Overrides for this target (optional)</legend>
          <div className="d-flex flex-wrap gap-3">
            <label>
              Visibility
              <select
                className="form-select mt-1"
                value={form.visibility}
                onChange={(event) => {
                  setForm({ ...form, visibility: event.target.value });
                }}
              >
                <option value="">As the template</option>
                <option value="private">private</option>
                <option value="internal">internal</option>
                <option value="public">public</option>
              </select>
            </label>
            <label>
              Default branch protected
              <select
                className="form-select mt-1"
                value={form.defaultBranchProtected}
                onChange={(event) => {
                  setForm({ ...form, defaultBranchProtected: event.target.value });
                }}
              >
                <option value="">As the template</option>
                <option value="yes">Yes</option>
                <option value="no">No</option>
              </select>
            </label>
            <label>
              Approvals required
              <input
                type="number"
                min={0}
                max={20}
                placeholder="As the template"
                className="form-control mt-1"
                value={form.minApprovals}
                onChange={(event) => {
                  setForm({ ...form, minApprovals: event.target.value });
                }}
              />
            </label>
          </div>
          {overrides !== undefined && (
            <label className="d-block fw-medium">
              {`Why this target differs (at least ${String(MIN_REASON)} characters)`}
              <textarea
                className="form-control mt-1"
                rows={2}
                maxLength={2000}
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                }}
              />
            </label>
          )}
        </fieldset>
        {failure && (
          <p role="alert" className="text-danger-emphasis">
            {failure}
          </p>
        )}
        <div>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={saving || !token || !choice || needsTarget || reasonShort}
          >
            {saving ? "Assigning…" : "Assign"}
          </button>
        </div>
      </div>
    </form>
  );
}

function Assignments({
  version,
  templates,
  onChanged,
}: {
  version: number;
  templates: TemplateSummary[];
  onChanged: (text: string) => void;
}) {
  const { token, canWrite } = useCsrf();
  const loaded = useLoad(`template-assignments#${String(version)}`, (signal) =>
    listTemplateAssignments(signal),
  );
  const [failure, setFailure] = useState<string | null>(null);

  async function remove(row: TemplateAssignment) {
    if (!token) return;
    setFailure(null);
    try {
      await unassignTemplate(row.id, token);
      onChanged(
        `Removed ${row.name} v${String(row.version)} from ${placeText(row.target).toLowerCase()}.`,
      );
    } catch (error) {
      setFailure(failureText(error, "The assignment could not be removed."));
    }
  }

  const columns: Column<TemplateAssignment>[] = [
    {
      key: "target",
      header: "Applies to",
      rowHeader: true,
      render: (row) => placeText(row.target),
    },
    {
      key: "template",
      header: "Template",
      render: (row) => `${row.name} v${String(row.version)}`,
    },
    {
      key: "overrides",
      header: "Overrides",
      priority: 2,
      render: (row) =>
        row.overrides ? (
          <>
            <span>{Object.keys(row.overrides).join(", ")}</span>
            <p className="small text-secondary mb-0">{row.override_reason}</p>
          </>
        ) : (
          <span className="text-secondary">None</span>
        ),
    },
    ...(canWrite
      ? [
          {
            key: "remove",
            header: "Actions",
            render: (row: TemplateAssignment) => (
              <button type="button" className="btn btn-sm" onClick={() => void remove(row)}>
                {`Remove from ${placeText(row.target).toLowerCase()}`}
              </button>
            ),
          },
        ]
      : []),
  ];

  return (
    <section aria-labelledby="template-assignments" className="stack-2">
      <h2 id="template-assignments" className="h3">
        Where templates apply
      </h2>
      <p className="text-secondary">
        For each setting, the nearest assignment that sets it decides: a project&apos;s own, then
        its group, then that group&apos;s parents, then the whole connection.
      </p>
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {!loaded.data && !loaded.error && <Loading rows={2} />}
      {loaded.data && loaded.data.assignments.length === 0 && (
        <EmptyState title="No template is assigned yet" />
      )}
      {loaded.data && loaded.data.assignments.length > 0 && (
        <ResponsiveTable
          columns={columns}
          rows={loaded.data.assignments}
          rowKey={(row) => row.id}
          caption="Template assignments"
        />
      )}
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
      {canWrite && templates.length > 0 && (
        <AssignForm templates={templates} onAssigned={onChanged} />
      )}
    </section>
  );
}

// --- effective settings ---

function EffectiveTable({ effective }: { effective: EffectiveSettings }) {
  if (effective.settings.length === 0) {
    return <EmptyState title="No template applies to this project" />;
  }
  const columns: Column<EffectiveSettings["settings"][number]>[] = [
    {
      key: "setting",
      header: "Setting",
      rowHeader: true,
      render: (row) => settingLabels[row.key] ?? row.key,
    },
    { key: "value", header: "Value", render: (row) => valueText(row.value) },
    {
      key: "source",
      header: "Decided by",
      render: (row) => (
        <>
          <span>{sourceText(row.decided_by)}</span>
          {row.replaced.length > 0 && (
            <p className="small text-secondary mb-0">
              {`Replaces ${row.replaced.map((s) => `${valueText(s.value)} from ${sourceText(s)}`).join("; ")}`}
            </p>
          )}
        </>
      ),
    },
    {
      key: "support",
      header: `On ${effective.provider === "github" ? "GitHub" : "GitLab"}`,
      priority: 2,
      render: (row) => (
        <>
          <span>{supportLabels[row.support]}</span>
          {row.support_note && <p className="small text-secondary mb-0">{row.support_note}</p>}
        </>
      ),
    },
  ];
  return (
    <ResponsiveTable
      columns={columns}
      rows={effective.settings}
      rowKey={(row) => row.key}
      caption={`Effective settings of ${effective.target.path}`}
    />
  );
}

function EffectiveLookup({
  version,
  chosen,
  onChoose,
  onChanged,
}: {
  version: number;
  chosen: ProjectRef | null;
  onChoose: (project: ProjectRef) => void;
  onChanged: (text: string) => void;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const [options, setOptions] = useState<Project[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const loaded = useLoad(
    `template-effective:${String(chosen?.id ?? 0)}#${String(version)}`,
    (signal) => (chosen ? getEffectiveSettings(chosen.id, signal) : Promise.resolve(null)),
  );

  async function search(event: FormEvent) {
    event.preventDefault();
    setFailure(null);
    try {
      const page = await listProjects(
        {
          search: text.trim(),
          state: "",
          visibility: "",
          archived: "",
          hasError: false,
          showExcluded: true,
          tag: "",
        },
        null,
      );
      setOptions(page.items);
    } catch (error) {
      setFailure(failureText(error, "The search failed."));
    }
  }

  return (
    <section aria-labelledby={`${id}-title`} className="stack-2">
      <h2 id={`${id}-title`} className="h3">
        A project&apos;s settings and drift
      </h2>
      <form
        onSubmit={(event) => void search(event)}
        className="d-flex flex-wrap align-items-end gap-2"
      >
        <label className="flex-grow-1 fw-medium">
          Find a project by path
          <input
            type="search"
            className="form-control mt-1"
            value={text}
            maxLength={200}
            placeholder="group/project"
            onChange={(event) => {
              setText(event.target.value);
            }}
          />
        </label>
        <button type="submit" className="btn">
          Search projects
        </button>
      </form>
      <div aria-live="polite">
        {failure && <p className="text-danger-emphasis">{failure}</p>}
        {options !== null && options.length === 0 && (
          <p className="text-secondary">No projects match.</p>
        )}
      </div>
      {options !== null && options.length > 0 && (
        <ul className="stack-1">
          {options.map((project) => (
            <li key={project.id}>
              <button
                type="button"
                className={`btn btn-link p-0${chosen?.id === project.id ? " fw-bold" : ""}`}
                aria-pressed={chosen?.id === project.id}
                onClick={() => {
                  onChoose({
                    id: project.id,
                    gitlab_id: project.gitlab_id,
                    path: project.path_with_namespace,
                  });
                }}
              >
                {project.path_with_namespace}
              </button>
            </li>
          ))}
        </ul>
      )}
      {chosen && loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {chosen && loaded.loading && <Loading rows={2} />}
      {chosen && loaded.data && (
        <>
          {loaded.data.ancestry === "incomplete" && (
            <p role="status" className="alert alert-warning text-body">
              Some groups above this project could not be followed, so templates assigned above them
              may be missing.
            </p>
          )}
          <EffectiveTable effective={loaded.data} />
          <DriftTable projectId={chosen.id} version={version} onChanged={onChanged} />
        </>
      )}
    </section>
  );
}

// --- support ---

function SupportTable() {
  const loaded = useLoad("template-settings", (signal) => listSettingSupport(signal));
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Loading rows={3} />;
  const cell = (support: Support, note: string | null) => (
    <>
      <span>{supportLabels[support]}</span>
      {note && <p className="small text-secondary mb-0">{note}</p>}
    </>
  );
  return (
    <ResponsiveTable
      columns={[
        {
          key: "setting",
          header: "Setting",
          rowHeader: true,
          render: (row) => (
            <>
              <span>{settingLabels[row.key] ?? row.key}</span>
              <p className="small text-secondary mb-0">
                <code>{row.key}</code>
              </p>
            </>
          ),
        },
        { key: "gitlab", header: "GitLab", render: (row) => cell(row.gitlab, row.gitlab_note) },
        { key: "github", header: "GitHub", render: (row) => cell(row.github, row.github_note) },
      ]}
      rows={loaded.data}
      rowKey={(row) => row.key}
      caption="Settings a template can hold"
      showCaption
    />
  );
}

export function TemplatesPage() {
  const { canWrite } = useCsrf();
  // Bumped after a change, so every list reloads.
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const [chosen, setChosen] = useState<ProjectRef | null>(null);
  const loaded = useLoad(`templates#${String(version)}`, (signal) => listTemplates(signal));

  useEffect(() => {
    if (message) status.current?.focus();
  }, [message]);

  function changed(text: string) {
    setMessage(text);
    setVersion((value) => value + 1);
  }

  const templates = loaded.data?.templates ?? [];
  return (
    <section aria-labelledby="page-title" className="stack-4">
      <GovernanceHeader intro="Templates name exactly how repositories should be configured. Assigning one does not change anything yet and does not make a project compliant: baselines still judge that." />
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="fw-medium">
          {message}
        </p>
      )}
      <section aria-labelledby="templates-title" className="stack-2">
        <h2 id="templates-title" className="h3">
          Templates
        </h2>
        {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
        {loaded.loading && <Loading rows={3} />}
        {loaded.data && templates.length === 0 && (
          <EmptyState title="No templates yet">
            {canWrite
              ? "Save the first version below."
              : "Someone who manages baselines can add one."}
          </EmptyState>
        )}
        <div className="stack-2">
          {templates.map((template) => (
            <TemplateCard key={`${template.name}#${String(version)}`} template={template} />
          ))}
        </div>
      </section>
      {canWrite && <SaveForm onSaved={changed} />}
      <Assignments version={version} templates={templates} onChanged={changed} />
      <AlignmentPanel version={version} onShow={setChosen} />
      <EffectiveLookup version={version} chosen={chosen} onChoose={setChosen} onChanged={changed} />
      <ExceptionsPanel version={version} onChanged={changed} />
      <section aria-labelledby="support-title" className="stack-2">
        <h2 id="support-title" className="h3">
          Settings and providers
        </h2>
        <p className="text-secondary">
          A template holds only settings Baluarte reads, so differences can be shown from evidence.
          Some have no equivalent on a provider or depend on its plan.
        </p>
        <SupportTable />
      </section>
    </section>
  );
}

export { valueText };
