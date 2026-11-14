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
import { secondaryClass } from "../components/styles";
import { canWriteTags, MANAGER, MANAGER_TAGS, writeFocus } from "../manager/format";
import { ManagerTabs, TagChip } from "../manager/parts";
import { TargetPicker, type Picked } from "../policies/TargetPicker";
import { asApiError, Problems } from "../settings/parts";
import { formatTime } from "../time";
import { PageHeader } from "../ui/PageHeader";
import { useDeviceClass } from "../ui/device";

function Header({ title }: { title: string }) {
  return (
    <div>
      <div>
        <PageHeader
          pretitle="Manager"
          title={title}
          subtitle={
            <>
              Baluarte&apos;s own tags group projects across the GitLab hierarchy, for example by
              team or cost centre. They never change GitLab and are not evidence of compliance.
            </>
          }
        />
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
      onCreated(`Tag ${tagText(created)} created. Assign it to groups or projects next.`);
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
      className="card card-body mb-3"
    >
      <h2 className="fw-semibold">New tag</h2>
      <Problems messages={problems} />
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="fw-medium">
          Key
          <input
            name="key"
            required
            maxLength={63}
            placeholder="team"
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium">
          Value
          <input
            name="value"
            required
            maxLength={63}
            placeholder="payments"
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium sm:col-span-2">
          Description (optional)
          <input name="description" maxLength={MAX_TAG_DESCRIPTION} className="form-control mt-1" />
        </label>
        <div className="d-flex align-items-end gap-3">
          <label className="d-flex align-items-center gap-2 fw-medium">
            <input name="use_color" type="checkbox" />
            Colour
          </label>
          <label className="fw-medium">
            <span className="visually-hidden">Colour value</span>
            <input name="color" type="color" defaultValue="#1f6feb" className="h-9 w-14" />
          </label>
        </div>
      </div>
      <p className="small text-secondary">
        Keys and values use lowercase letters, digits, dots, dashes or underscores. A project has
        one value per key.
      </p>
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Creating…" : "Create tag"}
      </button>
    </form>
  );
}

/** Every tag, with how many groups and projects it covers. */
export function TagsPage() {
  const { session } = useAuth();
  const device = useDeviceClass();
  // Phones read tags; creating, editing and assigning them needs a larger screen.
  const canWrite =
    canWriteTags(session.status === "signed_in" ? session.user : null) && device !== "phone";
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const tags = useLoad(`tags#${String(version)}`, (signal) => listTags(signal));

  return (
    <section aria-labelledby="page-title">
      <Header title="Tags" />
      {message && (
        <p role="status" className="alert alert-success text-body">
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
          <p className="text-secondary border border-dashed rounded p-3">No tags yet.</p>
        ) : (
          <div className="card table-responsive">
            <table className="table table-vcenter card-table mb-0">
              <caption className="visually-hidden">Tags</caption>
              <thead>
                <tr>
                  {["Tag", "Description", "Groups", "Projects covered", "Roll-up"].map((title) => (
                    <th key={title} scope="col" className="px-3 py-2 fw-medium">
                      {title}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {tags.data.items.map((tag) => (
                  <tr key={tag.id}>
                    <th scope="row" className="px-3 py-2 fw-medium">
                      <Link to={`${MANAGER_TAGS}/${String(tag.id)}`} aria-label={tagText(tag)}>
                        <TagChip tag={tag} color={tag.color} />
                      </Link>
                    </th>
                    <td className="px-3 py-2">
                      {tag.description ?? <span className="text-secondary">None</span>}
                    </td>
                    <td className="px-3 py-2">{tag.groups}</td>
                    <td className="px-3 py-2">{tag.projects}</td>
                    <td className="px-3 py-2">
                      <Link
                        to={`${MANAGER}?${writeFocus({ tag: tagParam(tag) }).toString()}`}
                        aria-label={`Overview for ${tagText(tag)}`}
                      >
                        Overview
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : (
        <p role="status" className="text-secondary">
          Loading tags…
        </p>
      )}
    </section>
  );
}

function EditTag({ tag, onSaved }: { tag: TagDetail; onSaved: (message: string) => void }) {
  const { session } = useAuth();
  const csrf = session.status === "signed_in" ? session.user.csrf_token : "";
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const description = String(data.get("description") ?? "").trim();
    const color = data.get("use_color") ? String(data.get("color") ?? "") : null;
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
      await updateTag(tag.id, { description: description || null, color }, csrf);
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
      className="card card-body mb-3"
    >
      <h2 className="fw-semibold">Description and colour</h2>
      <Problems messages={problems} />
      <label className="d-block fw-medium">
        Description
        <input
          name="description"
          defaultValue={tag.description ?? ""}
          maxLength={MAX_TAG_DESCRIPTION}
          className="form-control mt-1"
        />
      </label>
      <div className="d-flex align-items-end gap-3">
        <label className="d-flex align-items-center gap-2 fw-medium">
          <input name="use_color" type="checkbox" defaultChecked={tag.color !== null} />
          Colour
        </label>
        <label className="fw-medium">
          <span className="visually-hidden">Colour value</span>
          <input
            name="color"
            type="color"
            defaultValue={tag.color ?? "#1f6feb"}
            className="h-9 w-14"
          />
        </label>
      </div>
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Saving…" : "Save tag"}
      </button>
    </form>
  );
}

function AssignForm({ tag, onDone }: { tag: TagDetail; onDone: (message: string) => void }) {
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
      await assignTag(tag.id, [{ type: picked.type, gitlab_id: picked.gitlabId }], csrf);
      setPicked(null);
      onDone(`${tagText(tag)} assigned to ${picked.path}.`);
    } catch (error) {
      setProblems(failureMessages(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card card-body mb-3">
      <h2 className="fw-semibold">Assign</h2>
      <p className="text-secondary">
        A group&apos;s tag applies to its subgroups and projects. A value assigned to a project
        itself wins over one it inherits.
      </p>
      <Problems messages={problems} />
      <fieldset className="d-flex gap-3">
        <legend className="visually-hidden">Assign to</legend>
        {(["group", "project"] as const).map((option) => (
          <label key={option} className="d-flex align-items-center gap-2">
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
        className="btn btn-primary"
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
    <section aria-labelledby="assignments-title">
      <h2 id="assignments-title" className="card-title">
        Assigned to
      </h2>
      <Problems messages={problems} />
      {tag.assignments.length === 0 ? (
        <p className="text-secondary">Not assigned to any group or project yet.</p>
      ) : (
        <ul className="list-group">
          {tag.assignments.map((assignment) => (
            <li key={assignment.id} className="d-flex flex-wrap align-items-center gap-3 px-3 py-2">
              <span className="fw-medium">
                {assignment.target_type === "group" ? "Group " : "Project "}
                <Link
                  to={
                    assignment.target_type === "group"
                      ? groupHref(assignment.target_gitlab_id)
                      : projectHref(assignment.target_gitlab_id)
                  }
                >
                  {assignment.target_path}
                </Link>
              </span>
              <span className="text-secondary">
                {`by ${personName(assignment.assigned_by)}, ${formatTime(assignment.assigned_at)}`}
              </span>
              {canWrite && (
                <button
                  type="button"
                  onClick={() => void remove(assignment)}
                  aria-label={`Remove from ${assignment.target_path}`}
                  className={`ml-auto ${secondaryClass}`}
                >
                  Remove
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
  const device = useDeviceClass();
  // Phones read tags; creating, editing and assigning them needs a larger screen.
  const canWrite =
    canWriteTags(session.status === "signed_in" ? session.user : null) && device !== "phone";
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const tag = useLoad(`tag-${tagId}#${String(version)}`, (signal) => getTag(id, signal));

  function done(text: string) {
    setMessage(text);
    setVersion((value) => value + 1);
  }

  return (
    <section aria-labelledby="page-title">
      <Header title={tag.data ? `Tag ${tagText(tag.data)}` : "Tag"} />
      <p>
        <Link to={MANAGER_TAGS}>All tags</Link>
      </p>
      {message && (
        <p role="status" className="alert alert-success text-body">
          {message}
        </p>
      )}
      {tag.error ? (
        <ErrorMessage error={tag.error} retry={tag.retry} />
      ) : tag.data ? (
        <>
          <dl className="grid column-gap-3 gap-y-2 sm:grid-cols-[max-content_1fr]">
            <dt className="col-md-4 fw-medium text-secondary">Tag</dt>
            <dd>
              <TagChip tag={tag.data} color={tag.data.color} />
            </dd>
            <dt className="col-md-4 fw-medium text-secondary">Description</dt>
            <dd>{tag.data.description ?? "None"}</dd>
            <dt className="col-md-4 fw-medium text-secondary">Covers</dt>
            <dd>{`${String(tag.data.projects)} projects, through ${String(tag.data.groups)} groups and direct assignments`}</dd>
            <dt className="col-md-4 fw-medium text-secondary">Created</dt>
            <dd>{`${personName(tag.data.created_by)}, ${formatTime(tag.data.created_at)}`}</dd>
          </dl>
          <p>
            <Link to={`${MANAGER}?${writeFocus({ tag: tagParam(tag.data) }).toString()}`}>
              Overview for this tag
            </Link>
          </p>
          <Assignments tag={tag.data} canWrite={canWrite} onDone={done} />
          {canWrite && <AssignForm tag={tag.data} onDone={done} />}
          {canWrite && <EditTag key={String(version)} tag={tag.data} onSaved={done} />}
        </>
      ) : (
        <p role="status" className="text-secondary">
          Loading the tag…
        </p>
      )}
    </section>
  );
}
