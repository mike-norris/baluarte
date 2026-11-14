import { useId, useState, type KeyboardEvent } from "react";

import { ApiError } from "../api/client";
import { listGroups, listProjects, type InventoryFilters } from "../api/gitlab";

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
    // Policies can still be assigned to excluded targets; they apply once included again.
    showExcluded: true,
    tag: "",
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
    <div>
      <div className="d-flex flex-wrap align-items-end gap-2">
        <label className="grow fw-medium" htmlFor={`${id}-search`}>
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
            className="form-control mt-1"
          />
        </label>
        <button type="button" onClick={() => void run()} disabled={searching} className="btn">
          {searching ? "Searching…" : `Search ${noun}s`}
        </button>
      </div>
      <div aria-live="polite">
        {failure && <p className="text-danger-emphasis">{failure}</p>}
        {options !== null && options.length === 0 && (
          <p className="text-secondary">{`No active ${noun}s match.`}</p>
        )}
      </div>
      {options !== null && options.length > 0 && (
        <fieldset className="max-h-64 overflow-y-auto rounded border p-2">
          <legend className="px-1 fw-medium">
            {`Matching ${noun}s${options.length === SEARCH_LIMIT ? " (first 100; narrow the search)" : ""}`}
          </legend>
          <ul>
            {options.map((option) => (
              <li key={option.gitlabId}>
                <label className="d-flex align-items-center gap-2">
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
                  <span className="fw-medium">{option.path}</span>
                  <span className="text-secondary">{option.detail}</span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
      )}
      {value?.type === type && (
        <p>
          Selected: <span className="fw-medium">{value.path}</span>
        </p>
      )}
    </div>
  );
}
