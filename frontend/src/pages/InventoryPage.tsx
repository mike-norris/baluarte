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
import { FreshnessPanel } from "../freshness/FreshnessPanel";
import { PopulationNote } from "../freshness/PopulationNote";
import { isFiltered, readFilters, writeFilters, type InventoryView } from "../inventory/filters";
import { stateHelp, stateLabels, visibilityLabel } from "../inventory/format";
import { MonitoringScope } from "../inventory/MonitoringScope";
import { ScopeForm, type ScopeIntent } from "../inventory/ScopeForm";
import { TagForm, type TagIntent } from "../inventory/TagForm";
import { canWriteTags } from "../manager/format";
import { ItemTags } from "../manager/parts";
import { canWritePolicies } from "../policies/format";
import { formatTime } from "../time";
import { Badge, badgeClass, type Tone } from "../ui/Badge";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";
import { ListFooter } from "../ui/ListFooter";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

const stateTone: Record<InventoryState, Tone> = {
  active: "success",
  inaccessible: "warning",
  missing: "danger",
};

/** Columns hidden on phones, where each row stacks into a card. */
const PHONE_HIDDEN: ReadonlySet<string> = new Set(["Visibility", "Default branch", "Last seen"]);
const hide = (header: string) => (PHONE_HIDDEN.has(header) ? "d-none d-md-table-cell" : "");

function StateCell({ state }: { state: InventoryState }) {
  return (
    <td data-label="State">
      <span className={badgeClass(stateTone[state])} title={stateHelp[state]}>
        {stateLabels[state]}
      </span>
      {state !== "active" && <span className="visually-hidden">: {stateHelp[state]}</span>}
    </td>
  );
}

function ErrorCell({ error, at }: { error: string | null; at: string | null }) {
  return (
    <td data-label="Collection error">
      {error ? (
        <>
          <span className="text-danger-emphasis">{error}</span>
          <span className="d-block small text-secondary">{formatTime(at)}</span>
        </>
      ) : (
        <span className="text-secondary">None</span>
      )}
    </td>
  );
}

function ExcludedBadge({ label }: { label: string | null }) {
  if (!label) return null;
  return (
    <span className="ms-2">
      <Badge tone="secondary" title="Not evaluated, read in detail or counted.">
        {label}
      </Badge>
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
    <div className="table-responsive-md">
      <table className="table table-vcenter card-table table-mobile-md mb-0">
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {leading}
            {headers.map((header) => (
              <th key={header} scope="col" className={hide(header)}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
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
      <th scope="row" data-label="Project" className="fw-normal">
        <Link to={projectHref(project.gitlab_id)} className="fw-medium text-break">
          {project.path_with_namespace}
        </Link>
        {project.namespace_kind === "user" && (
          <span className="ms-2">
            <Badge tone="secondary">Personal</Badge>
          </span>
        )}
        {project.archived && (
          <span className="ms-2">
            <Badge tone="secondary" title="Archived projects are not checked by baselines.">
              Archived
            </Badge>
          </span>
        )}
        <ExcludedBadge
          label={monitoringLabel(project.monitoring, "project", project.path_with_namespace)}
        />
        <ItemTags tags={project.tags} />
      </th>
      <td data-label="Visibility" className={hide("Visibility")}>
        {visibilityLabel(project.visibility)}
      </td>
      <td data-label="Default branch" className={hide("Default branch")}>
        {project.default_branch ?? <span className="text-secondary">None (empty)</span>}
      </td>
      <StateCell state={project.state} />
      <td data-label="Last seen" className={`text-nowrap ${hide("Last seen")}`}>
        {formatTime(project.last_seen_at)}
      </td>
      <ErrorCell error={project.last_error} at={project.last_error_at} />
    </tr>
  ));
}

function GroupRows({ groups, select }: { groups: Group[]; select: SelectCell }) {
  return groups.map((group) => (
    <tr key={group.id}>
      {select?.(group.id, group.full_path)}
      <th scope="row" data-label="Group" className="fw-normal">
        <Link to={groupHref(group.gitlab_id)} className="fw-medium text-break">
          {group.full_path}
        </Link>
        <ExcludedBadge label={monitoringLabel(group.monitoring, "group", group.full_path)} />
        <ItemTags tags={group.tags} />
      </th>
      <td data-label="Visibility" className={hide("Visibility")}>
        {visibilityLabel(group.visibility)}
      </td>
      <StateCell state={group.state} />
      <td data-label="Last seen" className={`text-nowrap ${hide("Last seen")}`}>
        {formatTime(group.last_seen_at)}
      </td>
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
        tag: value("tag").replace("=", ":"),
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
      className="card-body border-bottom row g-2 align-items-end mx-0"
    >
      <label className="form-label col-12 col-md-6 col-xl-4 mb-0">
        Search path
        <input
          name="search"
          type="search"
          defaultValue={filters.search}
          maxLength={200}
          placeholder={view === "projects" ? "group/project" : "group/subgroup"}
          className="form-control mt-1"
        />
      </label>
      <label className="form-label col-6 col-md-3 col-xl-2 mb-0">
        State
        <select name="state" defaultValue={filters.state} className="form-select mt-1">
          <option value="">Any</option>
          <option value="active">Active</option>
          <option value="inaccessible">Inaccessible</option>
          <option value="missing">Missing</option>
        </select>
      </label>
      {view === "projects" && (
        <>
          <label className="form-label col-6 col-md-3 col-xl-2 mb-0">
            Visibility
            <select
              name="visibility"
              defaultValue={filters.visibility}
              className="form-select mt-1"
            >
              <option value="">Any</option>
              <option value="private">Private</option>
              <option value="internal">Internal</option>
              <option value="public">Public</option>
            </select>
          </label>
          <label className="form-label col-6 col-md-3 col-xl-2 mb-0">
            Archived
            <select name="archived" defaultValue={filters.archived} className="form-select mt-1">
              <option value="">Any</option>
              <option value="false">Not archived</option>
              <option value="true">Archived only</option>
            </select>
          </label>
        </>
      )}
      <label className="form-label col-6 col-md-3 col-xl-2 mb-0">
        Tag
        <input
          name="tag"
          defaultValue={filters.tag.replace(":", "=")}
          maxLength={127}
          placeholder="team=payments"
          className="form-control mt-1"
        />
      </label>
      <label className="form-check col-12 col-md-auto mb-0 ms-2">
        <input
          name="has_error"
          type="checkbox"
          defaultChecked={filters.hasError}
          className="form-check-input"
        />
        <span className="form-check-label">With collection errors</span>
      </label>
      <div className="col-12 btn-list">
        <button type="submit" className="btn">
          Apply filters
        </button>
        {isFiltered(filters) && (
          <Link
            to={`?${writeFilters(view, { ...emptyFilters, showExcluded: filters.showExcluded }).toString()}`}
            className="btn btn-ghost-secondary"
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
  tag: "",
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
      className="btn btn-sm"
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
  openTags,
  canScope,
  canTag,
}: {
  noun: string;
  type: "project" | "group";
  rows: T[];
  facts: (item: T) => RowFacts;
  selected: Set<number>;
  open: (intent: ScopeIntent) => void;
  openTags: (intent: TagIntent) => void;
  canScope: boolean;
  canTag: boolean;
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
      className="card-body border-bottom py-2 d-flex flex-wrap align-items-center gap-2"
    >
      <p aria-live="polite" className="mb-0 me-2">
        {`${count(chosen.length, type, `${type}s`)} selected.`}
      </p>
      {canScope && (
        <>
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
            className="btn btn-sm"
          >
            Exclude from monitoring
          </button>
          <button
            type="button"
            disabled={toInclude.length === 0}
            onClick={() => {
              open({ mode: "include", ids, labels: toInclude.map((row) => row.path) });
            }}
            className="btn btn-sm"
          >
            Include again
          </button>
        </>
      )}
      {canTag && (
        <button
          type="button"
          disabled={chosen.length === 0}
          onClick={() => {
            openTags({
              targets: chosen.map((row) => ({ type, gitlab_id: row.gitlabId })),
              labels: chosen.map((row) => row.path),
            });
          }}
          className="btn btn-sm"
        >
          Tag…
        </button>
      )}
      {canScope && toExclude.length > MAX_BATCH && (
        <p className="w-100 mb-0 text-secondary">
          {`At most ${String(MAX_BATCH)} can be excluded at once.`}
        </p>
      )}
      {canScope && elsewhere.length > 0 && (
        <p className="w-100 mb-0 text-secondary">
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
  canTag,
  onChanged,
}: {
  view: InventoryView;
  filters: InventoryFilters;
  load: (after: number | null, signal?: AbortSignal) => Promise<Page<T>>;
  render: (items: T[], select: SelectCell) => ReactNode;
  facts: (item: T) => RowFacts;
  headers: string[];
  canWrite: boolean;
  canTag: boolean;
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
  const device = useDeviceClass();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [intent, setIntent] = useState<ScopeIntent | null>(null);
  const [tagIntent, setTagIntent] = useState<TagIntent | null>(null);
  // No bulk actions on phones: rows are read-only there.
  const selectable = (canWrite || canTag) && device !== "phone";
  const noun = view === "projects" ? "projects" : "groups";
  const type = view === "projects" ? "project" : "group";

  if (first.error) {
    return (
      <div className="card-body">
        <ErrorMessage error={first.error} retry={first.retry} />
      </div>
    );
  }
  if (first.loading) {
    return (
      <div className="card-body">
        <Loading label={`Loading ${noun}…`} />
      </div>
    );
  }
  const hidden = filters.showExcluded ? undefined : first.data?.excluded_hidden;
  const hiddenNote =
    typeof hidden === "number" && hidden > 0 ? (
      <p className="text-secondary mb-0">
        {`${count(hidden, `excluded ${type}`, `excluded ${noun}`)} hidden. Select Show excluded to see ${hidden === 1 ? "it" : "them"}.`}
      </p>
    ) : null;
  if (paged.items.length === 0) {
    return (
      <EmptyState
        title={
          isFiltered(filters)
            ? `No ${noun} match these filters.`
            : hiddenNote
              ? `Every ${type} found is excluded from monitoring.`
              : `No ${noun} have been collected yet.`
        }
      >
        {hiddenNote}
      </EmptyState>
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
  const select: SelectCell = selectable
    ? (id, path) => (
        <td className="w-1">
          <input
            className="form-check-input m-0"
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
  const leading = selectable ? (
    <th scope="col" className="w-1">
      <input
        className="form-check-input m-0"
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
    <>
      {selectable && (
        <SelectionBar
          noun={noun}
          type={type}
          rows={paged.items}
          facts={facts}
          selected={selected}
          open={(next) => {
            setTagIntent(null);
            setIntent(next);
          }}
          openTags={(next) => {
            setIntent(null);
            setTagIntent(next);
          }}
          canScope={canWrite}
          canTag={canTag}
        />
      )}
      {tagIntent && (
        <div className="card-body border-bottom">
          <TagForm
            intent={tagIntent}
            onCancel={() => {
              setTagIntent(null);
            }}
            onDone={(message) => {
              setTagIntent(null);
              setSelected(new Set());
              onChanged(message);
            }}
          />
        </div>
      )}
      {intent && (
        <div className="card-body border-bottom">
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
        </div>
      )}
      <Table
        caption={view === "projects" ? "GitLab projects" : "GitLab groups"}
        headers={headers}
        leading={leading}
      >
        {render(paged.items, select)}
      </Table>
      <ListFooter
        noun={noun}
        shown={paged.items.length}
        hasMore={paged.next !== null}
        pending={paged.pending}
        error={paged.moreError}
        onMore={() => void paged.loadMore()}
      />
      {hiddenNote && <div className="card-body pt-0">{hiddenNote}</div>}
    </>
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
  const user = session.status === "signed_in" ? session.user : null;
  const canWrite = canWritePolicies(user);
  const canTag = canWriteTags(user);
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
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Observe"
        title="Inventory"
        subtitle="GitLab groups, subgroups and projects visible to Baluarte's read-only token."
      />
      <PopulationNote population={freshness.data?.population} />

      {freshness.error ? (
        <ErrorMessage error={freshness.error} retry={freshness.retry} />
      ) : freshness.data ? (
        <FreshnessPanel report={freshness.data} />
      ) : (
        <Loading label="Checking collection status…" rows={1} />
      )}

      {message && (
        <p role="status" className="alert alert-success text-body">
          {message}
        </p>
      )}

      <MonitoringScope reload={reload} onChanged={changed} />

      <div className="card">
        <div className="card-header">
          <nav aria-label="Inventory views">
            <ul className="nav nav-tabs card-header-tabs">
              {tabs.map((tab) => (
                <li key={tab.view} className="nav-item">
                  <Link
                    to={`?${writeFilters(tab.view, filters).toString()}`}
                    aria-current={tab.view === view ? "page" : undefined}
                    className={`nav-link ${tab.view === view ? "active" : ""}`}
                  >
                    {tab.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
        <Filters view={view} filters={filters} />
        <div className="card-body border-bottom py-2 d-flex flex-wrap align-items-center gap-3">
          <ExcludedToggle view={view} filters={filters} />
          <p className="text-secondary mb-0">
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
            canTag={canTag}
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
            canTag={canTag}
            onChanged={changed}
            headers={["Group", "Visibility", "State", "Last seen", "Collection error"]}
          />
        )}
      </div>
    </section>
  );
}
