import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import {
  getFreshness,
  groupHref,
  projectHref,
  listGroups,
  listProjects,
  type Group,
  type InventoryFilters,
  type InventoryState,
  type Page,
  type Project,
} from "../api/gitlab";
import { includableHere, monitoringLabel, MAX_BATCH, type Monitoring } from "../api/scope";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { FreshnessPanel } from "../freshness/FreshnessPanel";
import { PopulationNote } from "../freshness/PopulationNote";
import { isFiltered, readFilters, writeFilters, type InventoryView } from "../inventory/filters";
import { stateHelp, stateLabels, visibilityLabel } from "../inventory/format";
import { MonitoringScope } from "../inventory/MonitoringScope";
import { ScopeForm, type ScopeIntent } from "../inventory/ScopeForm";
import { canWritePolicies } from "../policies/format";
import { formatTime } from "../time";

function StateCell({ state }: { state: InventoryState }) {
  return (
    <td className="px-3 py-2">
      <span title={stateHelp[state]}>{stateLabels[state]}</span>
      {state !== "active" && <span className="sr-only">: {stateHelp[state]}</span>}
    </td>
  );
}

function ErrorCell({ error, at }: { error: string | null; at: string | null }) {
  return (
    <td className="px-3 py-2 text-red-800">
      {error ? (
        <>
          {error}
          <span className="block text-xs text-slate-600">{formatTime(at)}</span>
        </>
      ) : (
        <span className="text-slate-500">None</span>
      )}
    </td>
  );
}

function ExcludedBadge({ label }: { label: string | null }) {
  if (!label) return null;
  return (
    <span
      className="ml-2 rounded bg-violet-100 px-1.5 py-0.5 text-xs font-normal text-violet-950"
      title="Not evaluated, read in detail or counted."
    >
      {label}
    </span>
  );
}

function Table({
  caption,
  headers,
  leading,
  children,
}: {
  caption: string;
  headers: string[];
  /** A header cell before the others, such as select-all. */
  leading?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            {leading}
            {headers.map((header) => (
              <th key={header} scope="col" className="px-3 py-2 font-medium">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
    </div>
  );
}

/** The row checkbox, or nothing for users who cannot change the monitoring scope. */
type SelectCell = ((id: number, path: string) => ReactNode) | null;

function ProjectRows({ projects, select }: { projects: Project[]; select: SelectCell }) {
  return projects.map((project) => (
    <tr key={project.id}>
      {select?.(project.id, project.path_with_namespace)}
      <th scope="row" className="px-3 py-2 font-medium">
        <Link to={projectHref(project.gitlab_id)} className={linkClass}>
          {project.path_with_namespace}
        </Link>
        {project.namespace_kind === "user" && (
          <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 text-xs font-normal">
            Personal
          </span>
        )}
        {project.archived && (
          <span
            className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 text-xs font-normal"
            title="Archived projects are not checked by baselines."
          >
            Archived
          </span>
        )}
        <ExcludedBadge
          label={monitoringLabel(project.monitoring, "project", project.path_with_namespace)}
        />
      </th>
      <td className="px-3 py-2">{visibilityLabel(project.visibility)}</td>
      <td className="px-3 py-2">
        {project.default_branch ?? <span className="text-slate-500">None (empty)</span>}
      </td>
      <StateCell state={project.state} />
      <td className="px-3 py-2 whitespace-nowrap">{formatTime(project.last_seen_at)}</td>
      <ErrorCell error={project.last_error} at={project.last_error_at} />
    </tr>
  ));
}

function GroupRows({ groups, select }: { groups: Group[]; select: SelectCell }) {
  return groups.map((group) => (
    <tr key={group.id}>
      {select?.(group.id, group.full_path)}
      <th scope="row" className="px-3 py-2 font-medium">
        <Link to={groupHref(group.gitlab_id)} className={linkClass}>
          {group.full_path}
        </Link>
        <ExcludedBadge label={monitoringLabel(group.monitoring, "group", group.full_path)} />
      </th>
      <td className="px-3 py-2">{visibilityLabel(group.visibility)}</td>
      <StateCell state={group.state} />
      <td className="px-3 py-2 whitespace-nowrap">{formatTime(group.last_seen_at)}</td>
      <ErrorCell error={group.last_error} at={group.last_error_at} />
    </tr>
  ));
}

function Filters({ view, filters }: { view: InventoryView; filters: InventoryFilters }) {
  const [, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = (name: string) => {
      const entry = form.get(name);
      return typeof entry === "string" ? entry : "";
    };
    const { filters: parsed } = readFilters(
      new URLSearchParams({
        search: value("search"),
        state: value("state"),
        visibility: value("visibility"),
        archived: value("archived"),
        has_error: form.get("has_error") ? "true" : "false",
      }),
    );
    setParams(writeFilters(view, { ...parsed, showExcluded: filters.showExcluded }));
  }

  // Remount the form when the URL changes so its defaults follow navigation.
  const formKey = writeFilters(view, filters).toString();
  const noun = view === "projects" ? "projects" : "groups";

  return (
    <form
      key={formKey}
      onSubmit={apply}
      aria-label={`Filter ${noun}`}
      className="grid gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-2 lg:grid-cols-6 lg:items-end"
    >
      <label className="text-sm font-medium lg:col-span-2">
        Search path
        <input
          name="search"
          type="search"
          defaultValue={filters.search}
          maxLength={200}
          placeholder={view === "projects" ? "group/project" : "group/subgroup"}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        State
        <select name="state" defaultValue={filters.state} className={fieldClass}>
          <option value="">Any</option>
          <option value="active">Active</option>
          <option value="inaccessible">Inaccessible</option>
          <option value="missing">Missing</option>
        </select>
      </label>
      {view === "projects" && (
        <>
          <label className="text-sm font-medium">
            Visibility
            <select name="visibility" defaultValue={filters.visibility} className={fieldClass}>
              <option value="">Any</option>
              <option value="private">Private</option>
              <option value="internal">Internal</option>
              <option value="public">Public</option>
            </select>
          </label>
          <label className="text-sm font-medium">
            Archived
            <select name="archived" defaultValue={filters.archived} className={fieldClass}>
              <option value="">Any</option>
              <option value="false">Not archived</option>
              <option value="true">Archived only</option>
            </select>
          </label>
        </>
      )}
      <label className="flex items-center gap-2 text-sm font-medium">
        <input name="has_error" type="checkbox" defaultChecked={filters.hasError} />
        With collection errors
      </label>
      <div className="flex gap-2 sm:col-span-2 lg:col-span-6">
        <button type="submit" className={buttonClass}>
          Apply filters
        </button>
        {isFiltered(filters) && (
          <Link
            to={`?${writeFilters(view, { ...emptyFilters, showExcluded: filters.showExcluded }).toString()}`}
            className={secondaryClass}
          >
            Clear
          </Link>
        )}
      </div>
    </form>
  );
}

const emptyFilters: InventoryFilters = {
  search: "",
  state: "",
  visibility: "",
  archived: "",
  hasError: false,
  showExcluded: false,
};

/** Shows or hides what is excluded from monitoring; the choice is kept in the URL. */
function ExcludedToggle({ view, filters }: { view: InventoryView; filters: InventoryFilters }) {
  const [, setParams] = useSearchParams();
  const shown = filters.showExcluded;
  return (
    <button
      type="button"
      aria-pressed={shown}
      onClick={() => {
        setParams(writeFilters(view, { ...filters, showExcluded: !shown }));
      }}
      className={secondaryClass}
    >
      {shown ? "Hide excluded" : "Show excluded"}
    </button>
  );
}

/** What selection needs to know about a row. */
interface RowFacts {
  id: number;
  gitlabId: number;
  path: string;
  monitoring: Monitoring | undefined;
}

function count(n: number, one: string, many: string): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

/** The selected rows and what can be done with them. */
function SelectionBar<T>({
  noun,
  type,
  rows,
  facts,
  selected,
  open,
}: {
  noun: string;
  type: "project" | "group";
  rows: T[];
  facts: (item: T) => RowFacts;
  selected: Set<number>;
  open: (intent: ScopeIntent) => void;
}) {
  const chosen = rows.map(facts).filter((row) => selected.has(row.id));
  const toExclude = chosen.filter((row) => row.monitoring?.excluded !== true);
  const toInclude = chosen.filter((row) => includableHere(row.monitoring, type, row.path));
  const elsewhere = chosen.filter(
    (row) => row.monitoring?.excluded === true && !includableHere(row.monitoring, type, row.path),
  );
  const ids = [...new Set(toInclude.map((row) => row.monitoring?.exclusion_id ?? 0))];
  return (
    <div
      role="group"
      aria-label={`Selected ${noun}`}
      className="flex flex-wrap items-center gap-2 rounded border border-slate-200 bg-slate-50 p-2 text-sm"
    >
      <p aria-live="polite" className="mr-2">
        {`${count(chosen.length, type, `${type}s`)} selected.`}
      </p>
      <button
        type="button"
        disabled={toExclude.length === 0 || toExclude.length > MAX_BATCH}
        onClick={() => {
          open({
            mode: "exclude",
            targets: toExclude.map((row) => ({ type, gitlab_id: row.gitlabId })),
            labels: toExclude.map((row) => row.path),
          });
        }}
        className={secondaryClass}
      >
        Exclude from monitoring
      </button>
      <button
        type="button"
        disabled={toInclude.length === 0}
        onClick={() => {
          open({ mode: "include", ids, labels: toInclude.map((row) => row.path) });
        }}
        className={secondaryClass}
      >
        Include again
      </button>
      {toExclude.length > MAX_BATCH && (
        <p className="w-full text-slate-700">
          {`At most ${String(MAX_BATCH)} can be excluded at once.`}
        </p>
      )}
      {elsewhere.length > 0 && (
        <p className="w-full text-slate-700">
          {`${count(elsewhere.length, "selected " + type + " is", `selected ${type}s are`)} excluded by a group or the personal-namespace setting; include them again under Monitoring scope.`}
        </p>
      )}
    </div>
  );
}

function Listing<T>({
  view,
  filters,
  load,
  render,
  facts,
  headers,
  canWrite,
  onChanged,
}: {
  view: InventoryView;
  filters: InventoryFilters;
  load: (after: number | null, signal?: AbortSignal) => Promise<Page<T>>;
  render: (items: T[], select: SelectCell) => ReactNode;
  facts: (item: T) => RowFacts;
  headers: string[];
  canWrite: boolean;
  onChanged: (message: string) => void;
}) {
  const key = `${view}?${writeFilters(view, filters).toString()}`;
  const first = useLoad(key, (signal) => load(null, signal));
  const paged = usePaged<T, Page<T>>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => load(after),
  );
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [intent, setIntent] = useState<ScopeIntent | null>(null);
  const noun = view === "projects" ? "projects" : "groups";
  const type = view === "projects" ? "project" : "group";

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (first.loading) {
    return (
      <p role="status" className="text-slate-600">
        Loading {noun}…
      </p>
    );
  }
  const hidden = filters.showExcluded ? undefined : first.data?.excluded_hidden;
  const hiddenNote =
    typeof hidden === "number" && hidden > 0 ? (
      <p className="text-sm text-slate-600">
        {`${count(hidden, `excluded ${type}`, `excluded ${noun}`)} hidden. Select Show excluded to see ${hidden === 1 ? "it" : "them"}.`}
      </p>
    ) : null;
  if (paged.items.length === 0) {
    return (
      <div className="space-y-2">
        <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {isFiltered(filters)
            ? `No ${noun} match these filters.`
            : hiddenNote
              ? `Every ${type} found is excluded from monitoring.`
              : `No ${noun} have been collected yet.`}
        </p>
        {hiddenNote}
      </div>
    );
  }

  const ids = paged.items.map((item) => facts(item).id);
  const allSelected = ids.length > 0 && ids.every((id) => selected.has(id));
  function toggle(id: number) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  const select: SelectCell = canWrite
    ? (id, path) => (
        <td className="w-8 px-3 py-2">
          <input
            type="checkbox"
            checked={selected.has(id)}
            onChange={() => {
              toggle(id);
            }}
            aria-label={`Select ${path}`}
          />
        </td>
      )
    : null;
  const leading = canWrite ? (
    <th scope="col" className="w-8 px-3 py-2">
      <input
        type="checkbox"
        checked={allSelected}
        onChange={() => {
          setSelected(allSelected ? new Set() : new Set(ids));
        }}
        aria-label={`Select all ${noun} on this page`}
      />
    </th>
  ) : undefined;

  return (
    <div className="space-y-3">
      {canWrite && (
        <SelectionBar
          noun={noun}
          type={type}
          rows={paged.items}
          facts={facts}
          selected={selected}
          open={setIntent}
        />
      )}
      {intent && (
        <ScopeForm
          intent={intent}
          onCancel={() => {
            setIntent(null);
          }}
          onDone={(message) => {
            setIntent(null);
            setSelected(new Set());
            onChanged(message);
          }}
        />
      )}
      <Table
        caption={view === "projects" ? "GitLab projects" : "GitLab groups"}
        headers={headers}
        leading={leading}
      >
        {render(paged.items, select)}
      </Table>
      <p className="text-sm text-slate-600" aria-live="polite">
        Showing {paged.items.length} {noun}
        {paged.next !== null ? " (more available)" : ""}.
      </p>
      {hiddenNote}
      {paged.moreError && (
        <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
      )}
      {paged.next !== null && (
        <button
          type="button"
          onClick={() => void paged.loadMore()}
          disabled={paged.pending}
          className={secondaryClass}
        >
          {paged.pending ? "Loading…" : `Load more ${noun}`}
        </button>
      )}
    </div>
  );
}

const projectFacts = (project: Project): RowFacts => ({
  id: project.id,
  gitlabId: project.gitlab_id,
  path: project.path_with_namespace,
  monitoring: project.monitoring,
});

const groupFacts = (group: Group): RowFacts => ({
  id: group.id,
  gitlabId: group.gitlab_id,
  path: group.full_path,
  monitoring: group.monitoring,
});

export function InventoryPage() {
  const [params] = useSearchParams();
  const { view, filters } = readFilters(params);
  const freshness = useLoad("freshness", getFreshness);
  const { session } = useAuth();
  const canWrite = canWritePolicies(session.status === "signed_in" ? session.user : null);
  const [reload, setReload] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const tabs: { view: InventoryView; label: string }[] = [
    { view: "projects", label: "Projects" },
    { view: "groups", label: "Groups" },
  ];
  // A changed scope reloads the listing and clears the selection.
  const listingKey = `${view}?${writeFilters(view, filters).toString()}#${String(reload)}`;

  function changed(text: string) {
    setMessage(text);
    setReload((value) => value + 1);
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Inventory
        </h1>
        <p className="mt-2 text-slate-600">
          GitLab groups, subgroups and projects visible to Baluarte&apos;s read-only token.
        </p>
        <PopulationNote population={freshness.data?.population} />
      </div>

      {freshness.error ? (
        <ErrorMessage error={freshness.error} retry={freshness.retry} />
      ) : freshness.data ? (
        <FreshnessPanel report={freshness.data} />
      ) : (
        <p role="status" className="text-slate-600">
          Checking collection status…
        </p>
      )}

      {message && (
        <p role="status" className="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm">
          {message}
        </p>
      )}

      <MonitoringScope reload={reload} onChanged={changed} />

      <nav aria-label="Inventory views">
        <ul className="flex gap-1">
          {tabs.map((tab) => (
            <li key={tab.view}>
              <Link
                to={`?${writeFilters(tab.view, filters).toString()}`}
                aria-current={tab.view === view ? "page" : undefined}
                className={`block rounded px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${
                  tab.view === view
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-100"
                }`}
              >
                {tab.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <Filters view={view} filters={filters} />
      <div className="flex flex-wrap items-center gap-3">
        <ExcludedToggle view={view} filters={filters} />
        <p className="text-sm text-slate-600">
          {filters.showExcluded
            ? "Excluded projects and groups are shown with an Excluded badge."
            : "Projects and groups excluded from monitoring are hidden."}
        </p>
      </div>

      {view === "projects" ? (
        <Listing<Project>
          key={listingKey}
          view="projects"
          filters={filters}
          load={(after, signal) => listProjects(filters, after, signal)}
          render={(items, select) => <ProjectRows projects={items} select={select} />}
          facts={projectFacts}
          canWrite={canWrite}
          onChanged={changed}
          headers={[
            "Project",
            "Visibility",
            "Default branch",
            "State",
            "Last seen",
            "Collection error",
          ]}
        />
      ) : (
        <Listing<Group>
          key={listingKey}
          view="groups"
          filters={filters}
          load={(after, signal) => listGroups(filters, after, signal)}
          render={(items, select) => <GroupRows groups={items} select={select} />}
          facts={groupFacts}
          canWrite={canWrite}
          onChanged={changed}
          headers={["Group", "Visibility", "State", "Last seen", "Collection error"]}
        />
      )}
    </section>
  );
}
