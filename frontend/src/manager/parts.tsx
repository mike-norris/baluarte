import { useState } from "react";
import { NavLink, useSearchParams } from "react-router";

import type { Focus } from "../api/manager";
import { listTags, tagParam, tagText, type ItemTag } from "../api/tags";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { isHexColor } from "../labels/format";
import { TargetPicker, type Picked } from "../policies/TargetPicker";
import { focusText, managerTabs, readFocus, writeFocus } from "./format";


/** The manager space's sections the user may open; the focus is carried between them. */
export function ManagerTabs() {
  const { session } = useAuth();
  const [params] = useSearchParams();
  const user = session.status === "signed_in" ? session.user : null;
  const focus = writeFocus(readFocus(params)).toString();
  return (
    <nav aria-label="Manager space" className="mb-3">
      <ul className="nav nav-tabs flex-nowrap overflow-auto">
        {managerTabs
          .filter((tab) => tab.visible(user))
          .map((tab) => (
            <li key={tab.path} className="nav-item">
              <NavLink
                to={focus ? `${tab.path}?${focus}` : tab.path}
                end
                className="nav-link text-nowrap"
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
      </ul>
    </nav>
  );
}

/** A tag as ``key=value`` with its colour, never colour alone. */
export function TagChip({
  tag,
  color,
  title,
}: {
  tag: { key: string; value: string };
  color?: string | null;
  title?: string;
}) {
  return (
    <span
      title={title}
      className="badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle font-monospace fw-normal d-inline-flex align-items-center gap-1"
    >
      {color && isHexColor(color) && (
        <span
          aria-hidden="true"
          className="d-inline-block rounded-1 border"
          style={{ backgroundColor: color, width: "0.625rem", height: "0.625rem" }}
        />
      )}
      {tagText(tag)}
    </span>
  );
}

/** An item's tags, each saying where it comes from. */
export function ItemTags({ tags }: { tags: ItemTag[] | undefined }) {
  if (!tags || tags.length === 0) return null;
  return (
    <ul aria-label="Tags" className="list-unstyled mt-1 mb-0 d-flex flex-wrap gap-1">
      {tags.map((tag) => (
        <li key={`${tag.key}=${tag.value}`}>
          <TagChip
            tag={tag}
            title={tag.via === "group" ? `From group ${tag.from_path ?? ""}` : "On the project"}
          />
          {tag.via === "group" && <span className="visually-hidden"> (from group {tag.from_path})</span>}
        </li>
      ))}
    </ul>
  );
}

/** Roll the page up to everything, one group or one tag; the choice is kept in the URL. */
export function FocusPicker() {
  const [params, setParams] = useSearchParams();
  const focus = readFocus(params);
  const [mode, setMode] = useState<"group" | "tag">(focus.tag ? "tag" : "group");
  const [picked, setPicked] = useState<Picked | null>(null);
  const tags = useLoad("tags", (signal) => listTags(signal));

  function apply(next: Focus) {
    setParams(writeFocus(next, params));
  }

  return (
    <details className="rounded border p-3">
      <summary className="fw-medium">{`Showing ${focusText(focus)}. Change`}</summary>
      <div className="mt-3">
        <fieldset className="d-flex gap-3">
          <legend className="visually-hidden">Roll up by</legend>
          <label className="d-flex align-items-center gap-2">
            <input
              type="radio"
              name="focus-mode"
              checked={mode === "group"}
              onChange={() => {
                setMode("group");
              }}
            />
            Group
          </label>
          <label className="d-flex align-items-center gap-2">
            <input
              type="radio"
              name="focus-mode"
              checked={mode === "tag"}
              onChange={() => {
                setMode("tag");
              }}
            />
            Tag
          </label>
        </fieldset>
        {mode === "group" ? (
          <div >
            <TargetPicker type="group" value={picked} onChange={setPicked} />
            <button
              type="button"
              disabled={picked === null}
              onClick={() => {
                if (picked) apply({ group: picked.path });
              }}
              className="btn btn-primary"
            >
              Show this group
            </button>
          </div>
        ) : (
          <label className="d-block fw-medium">
            Tag
            <select
              value={focus.tag ?? ""}
              onChange={(event) => {
                apply(event.currentTarget.value ? { tag: event.currentTarget.value } : {});
              }}
              className="form-control mt-1"
            >
              <option value="">Choose a tag</option>
              {(tags.data?.items ?? []).map((tag) => (
                <option key={tag.id} value={tagParam(tag)}>
                  {tagText(tag)}
                </option>
              ))}
            </select>
          </label>
        )}
        {(focus.group ?? focus.tag) && (
          <button
            type="button"
            onClick={() => {
              apply({});
            }}
            className="btn"
          >
            Show all top-level groups
          </button>
        )}
      </div>
    </details>
  );
}
