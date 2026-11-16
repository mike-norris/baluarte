import { IconSearch } from "@tabler/icons-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router";

import {
  groupHref,
  listGroups,
  listProjects,
  projectHref,
  type InventoryFilters,
} from "../api/gitlab";
import { navTitle, type NavItem } from "../navigation";

/** The input's id, so the `/` shortcut can focus it. */
export const JUMP_SEARCH_ID = "jump-search";

interface Result {
  key: string;
  label: string;
  hint: string;
  to: string;
}

const SHOWN = 5;
const DEBOUNCE_MS = 250;

const searchFilters = (search: string): InventoryFilters => ({
  search,
  state: "active",
  visibility: "",
  archived: "",
  hasError: false,
  showExcluded: true,
  tag: "",
});

/** Records that can be opened by number, offered only when their section is visible. */
const byNumber: { section: string; label: string; href: (id: number) => string }[] = [
  { section: "/findings", label: "Finding", href: (id) => `/findings/${String(id)}` },
  {
    section: "/remediation",
    label: "Remediation plan",
    href: (id) => `/remediation/${String(id)}`,
  },
  { section: "/exceptions", label: "Exception", href: (id) => `/exceptions/${String(id)}` },
  { section: "/labels/plans", label: "Label plan", href: (id) => `/labels/plans/${String(id)}` },
];

function numberResults(query: string, sections: ReadonlySet<string>): Result[] {
  const match = /^#?(\d{1,9})$/.exec(query);
  if (!match) return [];
  const id = Number(match[1]);
  return byNumber
    .filter((entry) => sections.has(entry.section))
    .map((entry) => ({
      key: `${entry.section}#${String(id)}`,
      label: `${entry.label} #${String(id)}`,
      hint: "Open",
      to: entry.href(id),
    }));
}

function pageResults(query: string, items: readonly NavItem[]): Result[] {
  const needle = query.toLowerCase();
  return items
    .filter((item) => `${item.label} ${navTitle(item)}`.toLowerCase().includes(needle))
    .slice(0, SHOWN)
    .map((item) => ({
      key: `page:${item.path}`,
      label: navTitle(item),
      hint: "Page",
      to: item.path,
    }));
}

/**
 * Jump to a page by name, a record by `#id`, or a GitLab project or group by path. A combobox:
 * arrow keys move through the results, Enter opens one, Escape closes the list.
 */
export function JumpSearch({ items }: { items: readonly NavItem[] }) {
  const navigate = useNavigate();
  const listId = useId();
  const root = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [inventory, setInventory] = useState<{ query: string; results: Result[] } | null>(null);

  const sections = useMemo(() => new Set(items.map((item) => item.path)), [items]);
  const canSearchInventory = sections.has("/inventory");
  const trimmed = query.trim();
  const searchInventory = canSearchInventory && trimmed.length >= 2 && !trimmed.startsWith("#") && !/^\d+$/.test(trimmed);

  useEffect(() => {
    if (!searchInventory) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const filters = searchFilters(trimmed);
      Promise.all([
        listProjects(filters, null, controller.signal),
        listGroups(filters, null, controller.signal),
      ])
        .then(([projects, groups]) => {
          setInventory({
            query: trimmed,
            results: [
              ...groups.items.slice(0, SHOWN).map((group) => ({
                key: `group:${String(group.gitlab_id)}`,
                label: group.full_path,
                hint: "Group",
                to: groupHref(group.gitlab_id),
              })),
              ...projects.items.slice(0, SHOWN).map((project) => ({
                key: `project:${String(project.gitlab_id)}`,
                label: project.path_with_namespace,
                hint: "Project",
                to: projectHref(project.gitlab_id),
              })),
            ],
          });
        })
        .catch(() => {
          // A failed search adds no inventory results; the page and record results still stand.
          if (!controller.signal.aborted) setInventory({ query: trimmed, results: [] });
        });
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [searchInventory, trimmed]);

  const results: Result[] =
    trimmed === ""
      ? []
      : [
          ...numberResults(trimmed, sections),
          ...pageResults(trimmed, items),
          ...(searchInventory && inventory?.query === trimmed ? inventory.results : []),
        ];
  const pending = searchInventory && inventory?.query !== trimmed;
  const expanded = open && trimmed !== "";
  const current = Math.min(active, Math.max(results.length - 1, 0));

  useEffect(() => {
    if (!expanded) return;
    const onPointer = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [expanded]);

  function go(result: Result) {
    setOpen(false);
    setQuery("");
    void navigate(result.to);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      if (results.length === 0) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((current + step + results.length) % results.length);
    } else if (event.key === "Enter") {
      const result = results[current];
      if (expanded && result) {
        event.preventDefault();
        go(result);
      }
    } else if (event.key === "Escape") {
      if (expanded) {
        event.preventDefault();
        setOpen(false);
      } else {
        event.currentTarget.blur();
      }
    }
  }

  const optionId = (index: number) => `${listId}-${String(index)}`;

  return (
    <div ref={root} className="position-relative d-none d-md-block" style={{ width: "22rem" }}>
      <div className="input-icon">
        <span className="input-icon-addon">
          <IconSearch size={18} aria-hidden="true" />
        </span>
        <input
          id={JUMP_SEARCH_ID}
          type="search"
          role="combobox"
          aria-label="Jump to a page, record or project"
          aria-expanded={expanded}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={expanded && results.length > 0 ? optionId(current) : undefined}
          autoComplete="off"
          placeholder="Jump to project, finding #, page…"
          className="form-control"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
            setOpen(true);
          }}
          onFocus={() => {
            setOpen(true);
          }}
          onKeyDown={onKeyDown}
        />
        <span className="input-icon-addon" aria-hidden="true">
          <kbd>/</kbd>
        </span>
      </div>
      {expanded && (
        <div className="dropdown-menu show w-100 mt-1">
          <div id={listId} role="listbox" aria-label="Jump results" className="mb-0">
            {results.map((result, index) => (
              // The input keeps focus and handles keys (aria-activedescendant); a click picks.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events
              <div
                key={result.key}
                id={optionId(index)}
                role="option"
                tabIndex={-1}
                aria-selected={index === current}
                className={`dropdown-item d-flex justify-content-between gap-3 ${
                  index === current ? "active" : ""
                }`}
                onPointerDown={(event) => {
                  event.preventDefault();
                }}
                onClick={() => {
                  go(result);
                }}
              >
                <span className="text-truncate">{result.label}</span>
                <span className="text-secondary small text-nowrap">{result.hint}</span>
              </div>
            ))}
          </div>
          {results.length === 0 && !pending && (
            <p className="dropdown-item-text text-secondary mb-0">No matches.</p>
          )}
          {pending && (
            <p role="status" className="dropdown-item-text text-secondary small mb-0">
              Searching projects and groups…
            </p>
          )}
        </div>
      )}
    </div>
  );
}
