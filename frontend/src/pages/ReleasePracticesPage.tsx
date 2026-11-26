import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import type { TargetType } from "../api/policies";
import {
  assignPractice,
  createPractice,
  deletePractice,
  listPracticeAssignments,
  listPracticeProjects,
  listPractices,
  unassignPractice,
  updatePractice,
  type PracticeAssignment,
  type PracticeSettings,
  type PracticeStatus,
  type Preset,
  type PresetInfo,
  type ProductionFrom,
  type ProjectPractice,
  type ReleasePractice,
} from "../api/releasePractices";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { TargetPicker, type Picked } from "../policies/TargetPicker";
import { failureText, placeText, useCsrf } from "../templates/format";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

const fromLabels: Record<ProductionFrom, string> = {
  default_branch: "The default branch",
  tag: "Tags matching",
  branch: "Branches matching",
};

export const statusLabels: Record<PracticeStatus, string> = {
  follows: "Follows its practice",
  differs: "Differs",
  unknown: "Not known",
  no_releases: "No releases",
  unassigned: "No practice",
};

const statusBadges: Record<PracticeStatus, string> = {
  follows: "bg-success-subtle text-success-emphasis border border-success-subtle",
  differs: "bg-danger-subtle text-danger-emphasis border border-danger-subtle",
  unknown: "bg-warning-subtle text-warning-emphasis border border-warning-subtle",
  no_releases: "bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle",
  unassigned: "bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle",
};

/** A practice's parameters as one sentence per line. */
export function settingsLines(settings: PracticeSettings): string[] {
  const from =
    settings.production_from === "default_branch"
      ? "Production deploys the default branch."
      : `Production deploys ${settings.production_from === "tag" ? "tags" : "branches"} matching ${settings.ref_patterns.join(", ")}.`;
  const lines = [from, `Environments: ${settings.environments.join(", ")}.`];
  if (settings.required_approvals > 0) {
    lines.push(
      `${String(settings.required_approvals)} deployment ${settings.required_approvals === 1 ? "approval" : "approvals"} required.`,
    );
    lines.push(
      settings.creator_may_approve
        ? "Whoever started the release may approve it."
        : "Whoever started the release (for a tag, its creator) may not approve it.",
    );
    lines.push(
      settings.deployer_may_approve
        ? "The deployer may also approve."
        : "The deployer may not be an approver.",
    );
  }
  lines.push(
    settings.require_merge_request_review
      ? "Each change to the default branch is reviewed by someone other than its author."
      : "Merge request review is not required by this practice.",
  );
  return lines;
}

interface FormState {
  name: string;
  preset: Preset;
  description: string;
  productionFrom: ProductionFrom;
  patterns: string;
  environments: string;
  approvals: string;
  creatorMayApprove: boolean;
  deployerMayApprove: boolean;
  review: boolean;
}

function formFrom(preset: Preset, settings: PracticeSettings | null, name = ""): FormState {
  return {
    name,
    preset,
    description: "",
    productionFrom: settings?.production_from ?? "tag",
    patterns: (settings?.ref_patterns ?? ["*-release"]).join(", "),
    environments: (settings?.environments ?? ["production"]).join(", "),
    approvals: String(settings?.required_approvals ?? 1),
    creatorMayApprove: settings?.creator_may_approve ?? false,
    deployerMayApprove: settings?.deployer_may_approve ?? false,
    review: settings?.require_merge_request_review ?? true,
  };
}

function list(text: string): string[] {
  return text
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function settingsFrom(form: FormState): Partial<PracticeSettings> {
  const fromDefault = form.productionFrom === "default_branch";
  return {
    ...(form.preset === "custom" ? { production_from: form.productionFrom } : {}),
    ref_patterns: fromDefault ? [] : list(form.patterns),
    environments: list(form.environments),
    required_approvals: Number(form.approvals) || 0,
    creator_may_approve: form.creatorMayApprove,
    deployer_may_approve: form.deployerMayApprove,
    require_merge_request_review: form.review,
  };
}

function PracticeFields({
  form,
  setForm,
  editing,
}: {
  form: FormState;
  setForm: (form: FormState) => void;
  editing: boolean;
}) {
  const fromDefault = form.productionFrom === "default_branch";
  return (
    <>
      {form.preset === "custom" && !editing && (
        <label className="fw-medium">
          Production comes from
          <select
            className="form-select mt-1"
            value={form.productionFrom}
            onChange={(event) => {
              setForm({ ...form, productionFrom: event.target.value as ProductionFrom });
            }}
          >
            <option value="default_branch">The default branch</option>
            <option value="tag">Tags</option>
            <option value="branch">Branches</option>
          </select>
        </label>
      )}
      {!fromDefault && (
        <label className="fw-medium d-block">
          {`${fromLabels[form.productionFrom]} (comma-separated, * matches anything)`}
          <input
            className="form-control mt-1"
            value={form.patterns}
            onChange={(event) => {
              setForm({ ...form, patterns: event.target.value });
            }}
          />
        </label>
      )}
      <div className="d-flex flex-wrap gap-3">
        <label className="fw-medium">
          Production environments
          <input
            className="form-control mt-1"
            value={form.environments}
            onChange={(event) => {
              setForm({ ...form, environments: event.target.value });
            }}
          />
        </label>
        <label className="fw-medium">
          Deployment approvals required
          <input
            type="number"
            min={0}
            max={10}
            className="form-control mt-1"
            value={form.approvals}
            onChange={(event) => {
              setForm({ ...form, approvals: event.target.value });
            }}
          />
        </label>
      </div>
      <label className="form-check">
        <input
          type="checkbox"
          className="form-check-input"
          checked={!form.creatorMayApprove}
          onChange={(event) => {
            setForm({ ...form, creatorMayApprove: !event.target.checked });
          }}
        />
        <span className="form-check-label">
          Whoever started the release (for a tag, its creator) may not approve it
        </span>
      </label>
      <label className="form-check">
        <input
          type="checkbox"
          className="form-check-input"
          checked={!form.deployerMayApprove}
          onChange={(event) => {
            setForm({ ...form, deployerMayApprove: !event.target.checked });
          }}
        />
        <span className="form-check-label">The deployer may not be an approver</span>
        <span className="form-hint">
          The deployer is who GitLab records on the deployment. A deploy job that runs automatically
          after approval is recorded as whoever started the pipeline.
        </span>
      </label>
      <label className="form-check">
        <input
          type="checkbox"
          className="form-check-input"
          checked={form.review}
          onChange={(event) => {
            setForm({ ...form, review: event.target.checked });
          }}
        />
        <span className="form-check-label">
          Each change to the default branch is reviewed by someone other than its author
        </span>
      </label>
    </>
  );
}

function CreateForm({
  presets,
  onCreated,
}: {
  presets: PresetInfo[];
  onCreated: (text: string) => void;
}) {
  const id = useId();
  const { token } = useCsrf();
  const start = presets.find((p) => p.preset === "release_tag") ?? presets[0];
  const [form, setForm] = useState<FormState>(() =>
    formFrom(start?.preset ?? "release_tag", start?.settings ?? null),
  );
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function choose(preset: Preset) {
    const info = presets.find((p) => p.preset === preset);
    setForm({
      ...formFrom(preset, info?.settings ?? null, form.name),
      description: form.description,
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token || !form.name.trim()) return;
    setSaving(true);
    setFailure(null);
    try {
      const created = await createPractice(
        {
          name: form.name.trim(),
          preset: form.preset,
          description: form.description.trim() || null,
          settings: settingsFrom(form),
        },
        token,
      );
      onCreated(`Created the release practice ${created.name}.`);
      setForm(
        formFrom(form.preset, presets.find((p) => p.preset === form.preset)?.settings ?? null),
      );
    } catch (error) {
      setFailure(failureText(error, "The practice could not be created."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="card" aria-labelledby={`${id}-title`}>
      <div className="card-body stack-2">
        <h3 id={`${id}-title`} className="card-title">
          Add a release practice
        </h3>
        <fieldset className="stack-1">
          <legend className="fw-medium fs-5">Start from</legend>
          {presets.map((preset) => (
            <label key={preset.preset} className="form-check">
              <input
                type="radio"
                name={`${id}-preset`}
                className="form-check-input"
                checked={form.preset === preset.preset}
                onChange={() => {
                  choose(preset.preset);
                }}
              />
              <span className="form-check-label">{preset.label}</span>
            </label>
          ))}
        </fieldset>
        <div className="d-flex flex-wrap gap-3">
          <label className="fw-medium">
            Name
            <input
              className="form-control mt-1"
              value={form.name}
              pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?"
              maxLength={63}
              placeholder="tag-releases"
              onChange={(event) => {
                setForm({ ...form, name: event.target.value });
              }}
            />
          </label>
          <label className="fw-medium flex-grow-1">
            Description (optional)
            <input
              className="form-control mt-1"
              value={form.description}
              maxLength={2000}
              onChange={(event) => {
                setForm({ ...form, description: event.target.value });
              }}
            />
          </label>
        </div>
        <PracticeFields form={form} setForm={setForm} editing={false} />
        {failure && (
          <p role="alert" className="text-danger-emphasis">
            {failure}
          </p>
        )}
        <div>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={saving || !token || !form.name.trim()}
          >
            {saving ? "Saving…" : "Add practice"}
          </button>
        </div>
      </div>
    </form>
  );
}

function PracticeCard({
  practice,
  onChanged,
}: {
  practice: ReleasePractice;
  onChanged: (text: string) => void;
}) {
  const { token, canWrite } = useCsrf();
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<FormState>(() => ({
    ...formFrom(practice.preset, practice.settings, practice.name),
    description: practice.description ?? "",
  }));
  const [failure, setFailure] = useState<string | null>(null);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    setFailure(null);
    try {
      await updatePractice(
        practice.name,
        { description: form.description.trim() || null, settings: settingsFrom(form) },
        token,
      );
      setEditing(false);
      onChanged(`Saved the release practice ${practice.name}.`);
    } catch (error) {
      setFailure(failureText(error, "The practice could not be saved."));
    }
  }

  async function remove() {
    if (!token) return;
    setFailure(null);
    try {
      await deletePractice(practice.name, token);
      onChanged(`Deleted the release practice ${practice.name}.`);
    } catch (error) {
      setFailure(failureText(error, "The practice could not be deleted."));
    }
  }

  return (
    <article className="card" aria-label={practice.name}>
      <div className="card-body stack-2">
        <div className="d-flex flex-wrap justify-content-between gap-2">
          <div>
            <h3 className="card-title mb-0">{practice.name}</h3>
            <p className="text-secondary mb-0">{practice.preset_label}</p>
          </div>
          <span className="badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle align-self-start">
            {`Assigned in ${String(practice.assignments)} ${practice.assignments === 1 ? "place" : "places"}`}
          </span>
        </div>
        {practice.description && <p className="mb-0">{practice.description}</p>}
        {!editing && (
          <ul className="mb-0">
            {settingsLines(practice.settings).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
        {editing && (
          <form onSubmit={(event) => void save(event)} className="stack-2">
            <label className="fw-medium d-block">
              Description
              <input
                className="form-control mt-1"
                value={form.description}
                maxLength={2000}
                onChange={(event) => {
                  setForm({ ...form, description: event.target.value });
                }}
              />
            </label>
            <PracticeFields form={form} setForm={setForm} editing />
            <div className="d-flex gap-2">
              <button type="submit" className="btn btn-primary">
                Save
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setEditing(false);
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        )}
        {failure && (
          <p role="alert" className="text-danger-emphasis">
            {failure}
          </p>
        )}
        {canWrite && !editing && (
          <div className="d-flex gap-2">
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => {
                setEditing(true);
              }}
            >
              {`Edit ${practice.name}`}
            </button>
            {practice.assignments === 0 && (
              <button type="button" className="btn btn-sm" onClick={() => void remove()}>
                {`Delete ${practice.name}`}
              </button>
            )}
          </div>
        )}
        <p className="small text-secondary mb-0">
          {`Changed by ${practice.updated_by} on ${new Date(practice.updated_at).toLocaleString()}.`}
        </p>
      </div>
    </article>
  );
}

function AssignForm({
  practices,
  onAssigned,
}: {
  practices: ReleasePractice[];
  onAssigned: (text: string) => void;
}) {
  const id = useId();
  const { token } = useCsrf();
  const [name, setName] = useState(practices[0]?.name ?? "");
  const [targetType, setTargetType] = useState<TargetType>("instance");
  const [picked, setPicked] = useState<Picked | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const needsTarget = targetType !== "instance" && picked?.type !== targetType;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token || !name || needsTarget) return;
    setSaving(true);
    setFailure(null);
    try {
      const assigned = await assignPractice(
        {
          name,
          target_type: targetType,
          ...(targetType === "instance" || !picked ? {} : { target_id: picked.gitlabId }),
        },
        token,
      );
      onAssigned(`Assigned ${assigned.name} to ${placeText(assigned.target).toLowerCase()}.`);
    } catch (error) {
      setFailure(failureText(error, "The practice could not be assigned."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="card" aria-labelledby={`${id}-title`}>
      <div className="card-body stack-2">
        <h3 id={`${id}-title`} className="card-title">
          Assign a practice
        </h3>
        <p className="text-secondary mb-0">
          A connection, group or project holds one practice; assigning another replaces it.
        </p>
        <div className="d-flex flex-wrap gap-3">
          <label className="fw-medium">
            Practice
            <select
              className="form-select mt-1"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            >
              {practices.map((practice) => (
                <option key={practice.name} value={practice.name}>
                  {practice.name}
                </option>
              ))}
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
        {failure && (
          <p role="alert" className="text-danger-emphasis">
            {failure}
          </p>
        )}
        <div>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={saving || !token || !name || needsTarget}
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
  practices,
  onChanged,
}: {
  version: number;
  practices: ReleasePractice[];
  onChanged: (text: string) => void;
}) {
  const { token, canWrite } = useCsrf();
  const loaded = useLoad(`release-practice-assignments#${String(version)}`, (signal) =>
    listPracticeAssignments(signal),
  );
  const [failure, setFailure] = useState<string | null>(null);

  async function remove(row: PracticeAssignment) {
    if (!token) return;
    setFailure(null);
    try {
      await unassignPractice(row.id, token);
      onChanged(`Removed ${row.name} from ${placeText(row.target).toLowerCase()}.`);
    } catch (error) {
      setFailure(failureText(error, "The assignment could not be removed."));
    }
  }

  const columns: Column<PracticeAssignment>[] = [
    {
      key: "target",
      header: "Applies to",
      rowHeader: true,
      render: (row) => placeText(row.target),
    },
    { key: "practice", header: "Practice", render: (row) => row.name },
    { key: "by", header: "Assigned by", priority: 2, render: (row) => row.assigned_by },
    ...(canWrite
      ? [
          {
            key: "remove",
            header: "Actions",
            render: (row: PracticeAssignment) => (
              <button type="button" className="btn btn-sm" onClick={() => void remove(row)}>
                {`Remove from ${placeText(row.target).toLowerCase()}`}
              </button>
            ),
          },
        ]
      : []),
  ];

  return (
    <section aria-labelledby="practice-assignments" className="stack-2">
      <h2 id="practice-assignments" className="h3">
        Where practices apply
      </h2>
      <p className="text-secondary">
        The nearest assignment decides: a project&apos;s own, then its group, then that group&apos;s
        parents, then the whole connection.
      </p>
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {!loaded.data && !loaded.error && <Loading rows={2} />}
      {loaded.data && loaded.data.assignments.length === 0 && (
        <EmptyState title="No practice is assigned yet" />
      )}
      {loaded.data && loaded.data.assignments.length > 0 && (
        <ResponsiveTable
          columns={columns}
          rows={loaded.data.assignments}
          rowKey={(row) => row.id}
          caption="Release practice assignments"
        />
      )}
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
      {canWrite && practices.length > 0 && (
        <AssignForm practices={practices} onAssigned={onChanged} />
      )}
    </section>
  );
}

function observedText(row: ProjectPractice): string {
  const seen = row.observed;
  if (seen.deployments === 0) return "No production deployments";
  const kinds = Object.entries(seen.by_kind)
    .map(([kind, count]) => {
      const label =
        kind === "default_branch"
          ? "default branch"
          : kind === "tag"
            ? count === 1
              ? "tag"
              : "tags"
            : kind === "branch"
              ? count === 1
                ? "other branch"
                : "other branches"
              : "not known";
      return `${String(count)} ${label}`;
    })
    .join(", ");
  return `${String(seen.deployments)} ${seen.deployments === 1 ? "deployment" : "deployments"}: ${kinds}`;
}

const FILTERS: (PracticeStatus | "all")[] = [
  "all",
  "differs",
  "unknown",
  "follows",
  "no_releases",
  "unassigned",
];

function Projects({ version }: { version: number }) {
  const loaded = useLoad(`release-practice-projects#${String(version)}`, (signal) =>
    listPracticeProjects(signal),
  );
  const [filter, setFilter] = useState<PracticeStatus | "all">("all");
  const columns: Column<ProjectPractice>[] = [
    { key: "path", header: "Project", rowHeader: true, render: (row) => row.path },
    {
      key: "practice",
      header: "Practice",
      render: (row) =>
        row.practice ? (
          <>
            <span>{row.practice}</span>
            {row.decided_by && (
              <p className="small text-secondary mb-0">{`From ${placeText(row.decided_by).toLowerCase()}`}</p>
            )}
          </>
        ) : (
          <span className="text-secondary">None</span>
        ),
    },
    {
      key: "status",
      header: "Releases",
      render: (row) => (
        <span className={`badge ${statusBadges[row.status]}`}>{statusLabels[row.status]}</span>
      ),
    },
    {
      key: "observed",
      header: "What production deployed",
      priority: 2,
      render: (row) => (
        <>
          <span>{observedText(row)}</span>
          {row.observed.other_refs.length > 0 && (
            <p className="small text-secondary mb-0">
              {`Not what the practice expects: ${row.observed.other_refs.join(", ")}`}
            </p>
          )}
        </>
      ),
    },
  ];
  const data = loaded.data;
  const rows = data ? data.projects.filter((row) => filter === "all" || row.status === filter) : [];
  return (
    <section aria-labelledby="practice-projects" className="stack-2">
      <h2 id="practice-projects" className="h3">
        Projects and their releases
      </h2>
      {data && (
        <p className="text-secondary">
          {`Production deployments in the last ${String(data.window_days)} days, compared with each project's practice. Approvals and who released are judged by the release checks.`}
        </p>
      )}
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {!data && !loaded.error && <Loading rows={4} />}
      {data && (
        <>
          <label className="fw-medium">
            Show
            <select
              className="form-select mt-1 w-auto"
              value={filter}
              onChange={(event) => {
                setFilter(event.target.value as PracticeStatus | "all");
              }}
            >
              {FILTERS.map((value) => (
                <option key={value} value={value}>
                  {value === "all"
                    ? `All projects (${String(data.projects.length)})`
                    : `${statusLabels[value]} (${String(data.counts[value] ?? 0)})`}
                </option>
              ))}
            </select>
          </label>
          {rows.length === 0 ? (
            <EmptyState title="No projects to show" />
          ) : (
            <ResponsiveTable
              columns={columns}
              rows={rows}
              rowKey={(row) => row.project_id}
              caption="Projects and their release practices"
            />
          )}
        </>
      )}
    </section>
  );
}

export function ReleasePracticesPage() {
  const { canWrite } = useCsrf();
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const loaded = useLoad(`release-practices#${String(version)}`, (signal) => listPractices(signal));

  useEffect(() => {
    if (message) status.current?.focus();
  }, [message]);

  function changed(text: string) {
    setMessage(text);
    setVersion((value) => value + 1);
  }

  const practices = loaded.data?.practices ?? [];
  return (
    <section aria-labelledby="page-title" className="stack-4">
      <GovernanceHeader intro="Release practices say how projects are meant to reach production: from the default branch, a protected release tag with deployment approval, or a release branch. Baluarte compares each project's production deployments with its practice." />
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="fw-medium">
          {message}
        </p>
      )}
      <section aria-labelledby="practices-title" className="stack-2">
        <h2 id="practices-title" className="h3">
          Release practices
        </h2>
        {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
        {loaded.loading && <Loading rows={3} />}
        {loaded.data && practices.length === 0 && (
          <EmptyState title="No release practices yet">
            {canWrite
              ? "Add the first one below, starting from a preset."
              : "Someone who manages baselines can add one."}
          </EmptyState>
        )}
        <div className="stack-2">
          {practices.map((practice) => (
            <PracticeCard
              key={`${practice.name}#${String(version)}`}
              practice={practice}
              onChanged={changed}
            />
          ))}
        </div>
      </section>
      {canWrite && loaded.data && <CreateForm presets={loaded.data.presets} onCreated={changed} />}
      <Assignments version={version} practices={practices} onChanged={changed} />
      <Projects version={version} />
    </section>
  );
}
