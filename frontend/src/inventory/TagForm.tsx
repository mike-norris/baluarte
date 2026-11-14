import { useState } from "react";
import { Link } from "react-router";

import { errorReasons } from "../api/send";
import { assignTag, listTags, tagText, unassignTag, type TagTarget } from "../api/tags";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { MANAGER_TAGS } from "../manager/format";
import { asApiError, Problems } from "../settings/parts";

export interface TagIntent {
  targets: TagTarget[];
  /** The selected paths, for the confirmation. */
  labels: string[];
}

/** Add a Baluarte tag to the selected groups or projects, or take it off them. */
export function TagForm({
  intent,
  onCancel,
  onDone,
}: {
  intent: TagIntent;
  onCancel: () => void;
  onDone: (message: string) => void;
}) {
  const { session, refresh } = useAuth();
  const csrf = session.status === "signed_in" ? session.user.csrf_token : "";
  const tags = useLoad("tags", (signal) => listTags(signal));
  const [tagId, setTagId] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const chosen = tags.data?.items.find((tag) => String(tag.id) === tagId);
  const count = intent.targets.length;
  const noun = count === 1 ? "item" : "items";

  async function run(mode: "assign" | "unassign") {
    if (!chosen) {
      setProblems(["Choose a tag."]);
      return;
    }
    setSaving(true);
    setProblems([]);
    try {
      if (mode === "assign") await assignTag(chosen.id, intent.targets, csrf);
      else await unassignTag(chosen.id, intent.targets, csrf);
      onDone(
        mode === "assign"
          ? `${tagText(chosen)} added to ${String(count)} ${noun}.`
          : `${tagText(chosen)} taken off ${String(count)} ${noun} where it was assigned directly.`,
      );
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
      if (apiError.status === 401) void refresh();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div role="group" aria-labelledby="tag-form-title" className="rounded border p-3">
      <h2 id="tag-form-title" className="fw-semibold">
        {`Tag ${String(count)} selected ${noun}`}
      </h2>
      <ul className="ps-3 overflow-auto" style={{ maxHeight: "8rem" }}>
        {intent.labels.map((label) => (
          <li key={label}>{label}</li>
        ))}
      </ul>
      <Problems messages={problems} />
      {tags.error ? (
        <ErrorMessage error={tags.error} retry={tags.retry} />
      ) : tags.data && tags.data.items.length === 0 ? (
        <p className="text-secondary">
          No tags yet. <Link to={MANAGER_TAGS}>Create one under Manager, Tags</Link>.
        </p>
      ) : (
        <label className="d-block fw-medium">
          Tag
          <select
            value={tagId}
            onChange={(event) => {
              setTagId(event.currentTarget.value);
            }}
            className="form-control mt-1"
          >
            <option value="">{tags.data ? "Choose a tag" : "Loading tags…"}</option>
            {(tags.data?.items ?? []).map((tag) => (
              <option key={tag.id} value={String(tag.id)}>
                {tagText(tag)}
              </option>
            ))}
          </select>
        </label>
      )}
      <p className="small text-secondary">
        Tags are kept in Baluarte only; GitLab is not changed. A group&apos;s tag applies to its
        subgroups and projects.
      </p>
      <div className="d-flex flex-wrap gap-2">
        <button
          type="button"
          disabled={saving || !chosen}
          onClick={() => void run("assign")}
          className="btn btn-primary"
        >
          Add tag
        </button>
        <button
          type="button"
          disabled={saving || !chosen}
          onClick={() => void run("unassign")}
          className="btn"
        >
          Remove tag
        </button>
        <button type="button" onClick={onCancel} className="btn">
          Cancel
        </button>
      </div>
    </div>
  );
}
