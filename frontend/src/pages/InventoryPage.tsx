import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  getFreshness,
  listGroups,
  listProjects,
  type Group,
  type InventoryFilters,
  type InventoryState,
  type Page,
  type Project,
} from "../api/gitlab";
import { useLoad, type Loaded } from "../api/useLoad";
import { FreshnessPanel } from "../freshness/FreshnessPanel";
import {
  isFiltered,
  readFilters,
  writeFilters,
  type InventoryView,
} from "../inventory/filters";
import { stateHelp, stateLabels, visibilityLabel } from "../inventory/format";
import { formatTime } from "../time";

const buttonClass =
  "rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 disabled:opacity-50";
const secondaryClass =
  "rounded border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600";
const fieldClass =
  "mt-1 block w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-sky-600";

function ErrorMessage({ error, retry }: { error: ApiError; retry: () => void }) {
  return (
    <div role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-900">
      <p>{error.message}</p>
      {error.status !== 401 && error.status !== 403 && (
        <button type="button" onClick={retry} className={`mt-3 ${secondaryClass}`}>
          Try again
        </button>
      )}
    </div>
  );
}

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

function Table({ caption, headers, children }: { caption: string; headers: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
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

function ProjectRows({ projects }: { projects: Project[] }) {
  return projects.map((project) => (
    <tr key={project.id}>
      <th scope="row" className="px-3 py-2 font-medium">
        {project.path_with_namespace}
        {project.namespace_kind === "user" && (
          <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 text-xs font-normal">
            Personal
          </span>
        )}
        {project.archived && (
          <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 text-xs font-normal">
            Archived
          </span>
        )}
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

function GroupRows({ groups }: { groups: Group[] }) {
  return groups.map((group) => (
    <tr key={group.id}>
      <th scope="row" className="px-3 py-2 font-medium">
        {group.full_path}
      </th>
      <td className="px-3 py-2">{visibilityLabel(group.visibility)}</td>
      <StateCell state={group.state} />
      <td className="px-3 py-2 whitespace-nowrap">{formatTime(group.last_seen_at)}</td>
      <ErrorCell error={group.last_error} at={group.last_error_at} />
    </tr>
  ));
}

interface Extra<T> {
  key: string;
  items: T[];
  next: number | null;
}

/** First page from the hook, later pages appended by "Load more" for the same key. */
function usePaged<T>(key: string, first: Loaded<Page<T>>, fetchPage: (after: number) => Promise<Page<T>>) {
  const [extra, setExtra] = useState<Extra<T> | null>(null);
  const [pending, setPending] = useState(false);
  const [moreError, setMoreError] = useState<{ key: string; error: ApiError } | null>(null);
  const current = extra?.key === key ? extra : null;
  const items = first.data ? [...first.data.items, ...(current?.items ?? [])] : [];
  const next = current ? current.next : (first.data?.next_after_id ?? null);

  async function loadMore() {
    if (next === null) return;
    setPending(true);
    try {
      const page = await fetchPage(next);
      setExtra({ key, items: [...(current?.items ?? []), ...page.items], next: page.next_after_id });
      setMoreError(null);
    } catch (error) {
      setMoreError({
        key,
        error: error instanceof ApiError ? error : new ApiError(0, "The response could not be read."),
      });
    } finally {
      setPending(false);
    }
  }

  return {
    items,
    next,
    pending,
    moreError: moreError?.key === key ? moreError.error : undefined,
    loadMore,
  };
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
    setParams(writeFilters(view, parsed));
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
          <Link to={`?${writeFilters(view, emptyFilters).toString()}`} className={secondaryClass}>
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
};

function Listing<T>({
  view,
  filters,
  load,
  render,
  headers,
}: {
  view: InventoryView;
  filters: InventoryFilters;
  load: (after: number | null, signal?: AbortSignal) => Promise<Page<T>>;
  render: (items: T[]) => ReactNode;
  headers: string[];
}) {
  const key = `${view}?${writeFilters(view, filters).toString()}`;
  const first = useLoad(key, (signal) => load(null, signal));
  const paged = usePaged(key, first, (after) => load(after));
  const noun = view === "projects" ? "projects" : "groups";

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (first.loading) {
    return (
      <p role="status" className="text-slate-600">
        Loading {noun}…
      </p>
    );
  }
  if (paged.items.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {isFiltered(filters)
          ? `No ${noun} match these filters.`
          : `No ${noun} have been collected yet.`}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <Table caption={view === "projects" ? "GitLab projects" : "GitLab groups"} headers={headers}>
        {render(paged.items)}
      </Table>
      <p className="text-sm text-slate-600" aria-live="polite">
        Showing {paged.items.length} {noun}
        {paged.next !== null ? " (more available)" : ""}.
      </p>
      {paged.moreError && <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />}
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

export function InventoryPage() {
  const [params] = useSearchParams();
  const { view, filters } = readFilters(params);
  const freshness = useLoad("freshness", getFreshness);
  const tabs: { view: InventoryView; label: string }[] = [
    { view: "projects", label: "Projects" },
    { view: "groups", label: "Groups" },
  ];

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Inventory
        </h1>
        <p className="mt-2 text-slate-600">
          GitLab groups, subgroups and projects visible to Baluarte&apos;s read-only token.
        </p>
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

      <nav aria-label="Inventory views">
        <ul className="flex gap-1">
          {tabs.map((tab) => (
            <li key={tab.view}>
              <Link
                to={`?${writeFilters(tab.view, filters).toString()}`}
                aria-current={tab.view === view ? "page" : undefined}
                className={`block rounded px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${
                  tab.view === view ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"
                }`}
              >
                {tab.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <Filters view={view} filters={filters} />

      {view === "projects" ? (
        <Listing<Project>
          view="projects"
          filters={filters}
          load={(after, signal) => listProjects(filters, after, signal)}
          render={(items) => <ProjectRows projects={items} />}
          headers={["Project", "Visibility", "Default branch", "State", "Last seen", "Collection error"]}
        />
      ) : (
        <Listing<Group>
          view="groups"
          filters={filters}
          load={(after, signal) => listGroups(filters, after, signal)}
          render={(items) => <GroupRows groups={items} />}
          headers={["Group", "Visibility", "State", "Last seen", "Collection error"]}
        />
      )}
    </section>
  );
}
