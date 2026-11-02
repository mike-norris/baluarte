import { useId, useState, type KeyboardEvent } from "react";

import { ApiError } from "../api/client";
import { listGroups, listProjects, type InventoryFilters } from "../api/gitlab";
import { fieldClass, secondaryClass } from "../components/styles";

export interface Picked {
  type: "group" | "project";
  gitlabId: number;
  path: string;
}

interface Option {
  gitlabId: number;
  path: string;
  detail: string;
}

const SEARCH_LIMIT = 100;

function filters(search: string): InventoryFilters {
  return {
    search,
    state: "active",
    visibility: "",
    archived: "",
    hasError: false,
  };
}

async function search(type: "group" | "project", text: string): Promise<Option[]> {
  if (type === "group") {
    const page = await listGroups(filters(text), null);
    return page.items.map((group) => ({
      gitlabId: group.gitlab_id,
      path: group.full_path,
      detail: group.visibility ?? "visibility unknown",
    }));
  }
  const page = await listProjects(filters(text), null);
  return page.items.map((project) => ({
    gitlabId: project.gitlab_id,
    path: project.path_with_namespace,
    detail: [project.visibility ?? "visibility unknown", project.archived ? "archived" : ""]
      .filter(Boolean)
      .join(", "),
  }));
}

/**
 * Choose an active group or project by searching the inventory by path. Only active targets
 * are offered, because the API refuses to assign to a missing or inaccessible one.
 */
export function TargetPicker({
  type,
  value,
  onChange,
}: {
  type: "group" | "project";
  value: Picked | null;
  onChange: (picked: Picked | null) => void;
}) {
  const id = useId();
  const noun = type === "group" ? "group" : "project";
  const [text, setText] = useState("");
  const [options, setOptions] = useState<Option[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function run() {
    setSearching(true);
    setFailure(null);
    try {
      setOptions(await search(type, text.trim()));
    } catch (error) {
      setOptions(null);
      setFailure(error instanceof ApiError ? error.message : "The search failed.");
    } finally {
      setSearching(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    // Searching must not submit the surrounding form.
    if (event.key === "Enter") {
      event.preventDefault();
      void run();
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end gap-2">
        <label className="grow text-sm font-medium" htmlFor={`${id}-search`}>
          {`Find a ${noun} by path`}
          <input
            id={`${id}-search`}
            type="search"
            value={text}
            maxLength={200}
            placeholder={type === "group" ? "group/subgroup" : "group/project"}
            onChange={(event) => {
              setText(event.target.value);
            }}
            onKeyDown={onKeyDown}
            className={fieldClass}
          />
        </label>
        <button
          type="button"
          onClick={() => void run()}
          disabled={searching}
          className={secondaryClass}
        >
          {searching ? "Searching…" : `Search ${noun}s`}
        </button>
      </div>
      <div aria-live="polite">
        {failure && <p className="text-sm text-red-900">{failure}</p>}
        {options !== null && options.length === 0 && (
          <p className="text-sm text-slate-600">{`No active ${noun}s match.`}</p>
        )}
      </div>
      {options !== null && options.length > 0 && (
        <fieldset className="max-h-64 overflow-y-auto rounded border border-slate-200 p-2">
          <legend className="px-1 text-sm font-medium">
            {`Matching ${noun}s${options.length === SEARCH_LIMIT ? " (first 100; narrow the search)" : ""}`}
          </legend>
          <ul className="space-y-1">
            {options.map((option) => (
              <li key={option.gitlabId}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name={`${id}-target`}
                    checked={value?.gitlabId === option.gitlabId && value.type === type}
                    onChange={() => {
                      onChange({
                        type,
                        gitlabId: option.gitlabId,
                        path: option.path,
                      });
                    }}
                  />
                  <span className="font-medium">{option.path}</span>
                  <span className="text-slate-600">{option.detail}</span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
      )}
      {value?.type === type && (
        <p className="text-sm">
          Selected: <span className="font-medium">{value.path}</span>
        </p>
      )}
    </div>
  );
}
