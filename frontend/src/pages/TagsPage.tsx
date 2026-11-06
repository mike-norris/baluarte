import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { groupHref, projectHref } from "../api/gitlab";
import { personName } from "../api/manager";
import { errorReasons } from "../api/send";
import {
  assignTag,
  createTag,
  getTag,
  listTags,
  MAX_TAG_DESCRIPTION,
  tagParam,
  tagProblems,
  tagText,
  unassignTag,
  updateTag,
  type NewTag,
  type TagAssignment,
  type TagDetail,
  type TagTargetType,
} from "../api/tags";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  buttonClass,
  fieldClass,
  linkClass,
  secondaryClass,
} from "../components/styles";
import {
  canWriteTags,
  MANAGER,
  MANAGER_TAGS,
  writeFocus,
} from "../manager/format";
import { ManagerTabs, TagChip } from "../manager/parts";
import { TargetPicker, type Picked } from "../policies/TargetPicker";
import { asApiError, Problems } from "../settings/parts";
import { formatTime } from "../time";

function Header({ title }: { title: string }) {
  return (
    <div className="space-y-3">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          {title}
        </h1>
        <p className="mt-2 text-slate-600">
          Baluarte&apos;s own tags group projects across the GitLab hierarchy,
          for example by team or cost centre. They never change GitLab and are
          not evidence of compliance.
        </p>
      </div>
      <ManagerTabs />
    </div>
  );
}

function failureMessages(error: unknown): string[] {
  const apiError = asApiError(error);
  return [apiError.message, ...errorReasons(apiError)];
}

function NewTagForm({ onCreated }: { onCreated: (message: string) => void }) {
  const { session, refresh } = useAuth();
  const csrf = session.status === "signed_in" ? session.user.csrf_token : "";
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const value = (name: string) => {
      const entry = data.get(name);
      return typeof entry === "string" ? entry.trim() : "";
    };
    const tag: NewTag = { key: value("key"), value: value("value") };
    if (value("description")) tag.description = value("description");
    if (value("color") && data.get("use_color")) tag.color = value("color");
    const found = Object.values(tagProblems(tag));
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const created = await createTag(tag, csrf);
      form.reset();
      onCreated(
        `Tag ${tagText(created)} created. Assign it to groups or projects next.`,
      );
    } catch (error) {
      setProblems(failureMessages(error));
      if (asApiError(error).status === 401) void refresh();
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="New tag"
      noValidate
      className="space-y-3 rounded border border-slate-200 bg-white p-4"
    >
      <h2 className="font-semibold">New tag</h2>
      <Problems messages={problems} />
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-sm font-medium">
          Key
          <input
            name="key"
            required
            maxLength={63}
            placeholder="team"
            className={fieldClass}
          />
        </label>
        <label className="text-sm font-medium">
          Value
          <input
            name="value"
            required
            maxLength={63}
            placeholder="payments"
            className={fieldClass}
          />
        </label>
        <label className="text-sm font-medium sm:col-span-2">
          Description (optional)
          <input
            name="description"
            maxLength={MAX_TAG_DESCRIPTION}
            className={fieldClass}
          />
        </label>
        <div className="flex items-end gap-3">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input name="use_color" type="checkbox" />
            Colour
          </label>
          <label className="text-sm font-medium">
            <span className="sr-only">Colour value</span>
            <input
              name="color"
              type="color"
              defaultValue="#1f6feb"
              className="h-9 w-14"
            />
          </label>
        </div>
      </div>
      <p className="text-xs text-slate-600">
        Keys and values use lowercase letters, digits, dots, dashes or
        underscores. A project has one value per key.
      </p>
      <button type="submit" disabled={saving} className={buttonClass}>
        {saving ? "Creating…" : "Create tag"}
      </button>
    </form>
  );
}

/** Every tag, with how many groups and projects it covers. */
export function TagsPage() {
  const { session } = useAuth();
  const canWrite = canWriteTags(
    session.status === "signed_in" ? session.user : null,
  );
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const tags = useLoad(`tags#${String(version)}`, (signal) => listTags(signal));

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <Header title="Tags" />
      {message && (
        <p
          role="status"
          className="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm"
        >
          {message}
        </p>
      )}
      {canWrite && (
        <NewTagForm
          onCreated={(text) => {
            setMessage(text);
            setVersion((value) => value + 1);
          }}
        />
      )}
      {tags.error ? (
        <ErrorMessage error={tags.error} retry={tags.retry} />
      ) : tags.data ? (
        tags.data.items.length === 0 ? (
          <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
            No tags yet.
          </p>
        ) : (
          <div className="overflow-x-auto rounded border border-slate-200 bg-white">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Tags</caption>
              <thead className="border-b border-slate-200 bg-slate-50">
                <tr>
                  {[
                    "Tag",
                    "Description",
                    "Groups",
                    "Projects covered",
                    "Roll-up",
                  ].map((title) => (
                    <th
                      key={title}
                      scope="col"
                      className="px-3 py-2 font-medium"
                    >
                      {title}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {tags.data.items.map((tag) => (
                  <tr key={tag.id}>
                    <th scope="row" className="px-3 py-2 font-medium">
                      <Link
                        to={`${MANAGER_TAGS}/${String(tag.id)}`}
                        aria-label={tagText(tag)}
                        className="hover:underline"
                      >
                        <TagChip tag={tag} color={tag.color} />
                      </Link>
                    </th>
                    <td className="px-3 py-2">
                      {tag.description ?? (
                        <span className="text-slate-500">None</span>
                      )}
                    </td>
                    <td className="px-3 py-2">{tag.groups}</td>
                    <td className="px-3 py-2">{tag.projects}</td>
                    <td className="px-3 py-2">
                      <Link
                        to={`${MANAGER}?${writeFocus({ tag: tagParam(tag) }).toString()}`}
                        className={linkClass}
                      >
                        Overview
                        <span className="sr-only">{` for ${tagText(tag)}`}</span>
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : (
        <p role="status" className="text-slate-600">
          Loading tags…
        </p>
      )}
    </section>
  );
}

function EditTag({
  tag,
  onSaved,
}: {
  tag: TagDetail;
  onSaved: (message: string) => void;
}) {
  const { session } = useAuth();
  const csrf = session.status === "signed_in" ? session.user.csrf_token : "";
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const description = String(data.get("description") ?? "").trim();
    const color = data.get("use_color")
      ? String(data.get("color") ?? "")
      : null;
    const found = Object.values(
      tagProblems({
        key: tag.key,
        value: tag.value,
        description,
        ...(color ? { color } : {}),
      }),
    );
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      await updateTag(
        tag.id,
        { description: description || null, color },
        csrf,
      );
      onSaved("Tag saved.");
    } catch (error) {
      setProblems(failureMessages(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="Edit tag"
      noValidate
      className="space-y-3 rounded border border-slate-200 bg-white p-4"
    >
      <h2 className="font-semibold">Description and colour</h2>
      <Problems messages={problems} />
      <label className="block text-sm font-medium">
        Description
        <input
          name="description"
          defaultValue={tag.description ?? ""}
          maxLength={MAX_TAG_DESCRIPTION}
          className={fieldClass}
        />
      </label>
      <div className="flex items-end gap-3">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            name="use_color"
            type="checkbox"
            defaultChecked={tag.color !== null}
          />
          Colour
        </label>
        <label className="text-sm font-medium">
          <span className="sr-only">Colour value</span>
          <input
            name="color"
            type="color"
            defaultValue={tag.color ?? "#1f6feb"}
            className="h-9 w-14"
          />
        </label>
      </div>
      <button type="submit" disabled={saving} className={buttonClass}>
        {saving ? "Saving…" : "Save tag"}
      </button>
    </form>
  );
}

function AssignForm({
  tag,
  onDone,
}: {
  tag: TagDetail;
  onDone: (message: string) => void;
}) {
  const { session } = useAuth();
  const csrf = session.status === "signed_in" ? session.user.csrf_token : "";
  const [type, setType] = useState<TagTargetType>("group");
  const [picked, setPicked] = useState<Picked | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function assign() {
    if (!picked) return;
    setSaving(true);
    setProblems([]);
    try {
      await assignTag(
        tag.id,
        [{ type: picked.type, gitlab_id: picked.gitlabId }],
        csrf,
      );
      setPicked(null);
      onDone(`${tagText(tag)} assigned to ${picked.path}.`);
    } catch (error) {
      setProblems(failureMessages(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3 rounded border border-slate-200 bg-white p-4">
      <h2 className="font-semibold">Assign</h2>
      <p className="text-sm text-slate-600">
        A group&apos;s tag applies to its subgroups and projects. A value
        assigned to a project itself wins over one it inherits.
      </p>
      <Problems messages={problems} />
      <fieldset className="flex gap-4 text-sm">
        <legend className="sr-only">Assign to</legend>
        {(["group", "project"] as const).map((option) => (
          <label key={option} className="flex items-center gap-2">
            <input
              type="radio"
              name="assign-type"
              checked={type === option}
              onChange={() => {
                setType(option);
                setPicked(null);
              }}
            />
            {option === "group" ? "Group" : "Project"}
          </label>
        ))}
      </fieldset>
      <TargetPicker type={type} value={picked} onChange={setPicked} />
      <button
        type="button"
        disabled={picked === null || saving}
        onClick={() => void assign()}
        className={buttonClass}
      >
        {saving ? "Assigning…" : `Assign ${tagText(tag)}`}
      </button>
    </div>
  );
}

function Assignments({
  tag,
  canWrite,
  onDone,
}: {
  tag: TagDetail;
  canWrite: boolean;
  onDone: (message: string) => void;
}) {
  const { session } = useAuth();
  const csrf = session.status === "signed_in" ? session.user.csrf_token : "";
  const [problems, setProblems] = useState<string[]>([]);

  async function remove(assignment: TagAssignment) {
    setProblems([]);
    try {
      await unassignTag(
        tag.id,
        [
          {
            type: assignment.target_type,
            gitlab_id: assignment.target_gitlab_id,
          },
        ],
        csrf,
      );
      onDone(`${tagText(tag)} removed from ${assignment.target_path}.`);
    } catch (error) {
      setProblems(failureMessages(error));
    }
  }

  return (
    <section aria-labelledby="assignments-title" className="space-y-3">
      <h2 id="assignments-title" className="text-lg font-semibold">
        Assigned to
      </h2>
      <Problems messages={problems} />
      {tag.assignments.length === 0 ? (
        <p className="text-sm text-slate-600">
          Not assigned to any group or project yet.
        </p>
      ) : (
        <ul className="divide-y divide-slate-100 rounded border border-slate-200 bg-white">
          {tag.assignments.map((assignment) => (
            <li
              key={assignment.id}
              className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm"
            >
              <span className="font-medium">
                {assignment.target_type === "group" ? "Group " : "Project "}
                <Link
                  to={
                    assignment.target_type === "group"
                      ? groupHref(assignment.target_gitlab_id)
                      : projectHref(assignment.target_gitlab_id)
                  }
                  className={linkClass}
                >
                  {assignment.target_path}
                </Link>
              </span>
              <span className="text-slate-600">
                {`by ${personName(assignment.assigned_by)}, ${formatTime(assignment.assigned_at)}`}
              </span>
              {canWrite && (
                <button
                  type="button"
                  onClick={() => void remove(assignment)}
                  className={`ml-auto ${secondaryClass}`}
                >
                  Remove
                  <span className="sr-only">{` from ${assignment.target_path}`}</span>
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** One tag: what it covers, where it is assigned, and (with permission) changing that. */
export function TagPage() {
  const { tagId = "" } = useParams();
  const id = Number(tagId);
  const { session } = useAuth();
  const canWrite = canWriteTags(
    session.status === "signed_in" ? session.user : null,
  );
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const tag = useLoad(`tag-${tagId}#${String(version)}`, (signal) =>
    getTag(id, signal),
  );

  function done(text: string) {
    setMessage(text);
    setVersion((value) => value + 1);
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <Header title={tag.data ? `Tag ${tagText(tag.data)}` : "Tag"} />
      <p className="text-sm">
        <Link to={MANAGER_TAGS} className={linkClass}>
          All tags
        </Link>
      </p>
      {message && (
        <p
          role="status"
          className="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm"
        >
          {message}
        </p>
      )}
      {tag.error ? (
        <ErrorMessage error={tag.error} retry={tag.retry} />
      ) : tag.data ? (
        <>
          <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
            <dt className="font-medium text-slate-700">Tag</dt>
            <dd>
              <TagChip tag={tag.data} color={tag.data.color} />
            </dd>
            <dt className="font-medium text-slate-700">Description</dt>
            <dd>{tag.data.description ?? "None"}</dd>
            <dt className="font-medium text-slate-700">Covers</dt>
            <dd>{`${String(tag.data.projects)} projects, through ${String(tag.data.groups)} groups and direct assignments`}</dd>
            <dt className="font-medium text-slate-700">Created</dt>
            <dd>{`${personName(tag.data.created_by)}, ${formatTime(tag.data.created_at)}`}</dd>
          </dl>
          <p className="text-sm">
            <Link
              to={`${MANAGER}?${writeFocus({ tag: tagParam(tag.data) }).toString()}`}
              className={linkClass}
            >
              Overview for this tag
            </Link>
          </p>
          <Assignments tag={tag.data} canWrite={canWrite} onDone={done} />
          {canWrite && <AssignForm tag={tag.data} onDone={done} />}
          {canWrite && (
            <EditTag key={String(version)} tag={tag.data} onSaved={done} />
          )}
        </>
      ) : (
        <p role="status" className="text-slate-600">
          Loading the tag…
        </p>
      )}
    </section>
  );
}
