import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import {
  changeWorkspace,
  choose,
  createWorkspace,
  listWorkspaces,
  WORKSPACE_SLUG,
  type WorkspaceInfo,
  type WorkspaceRole,
} from "../api/workspace";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { asApiError, Problems } from "../settings/parts";
import { Badge } from "../ui/Badge";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { PageHeader } from "../ui/PageHeader";

const ROLES: { role: WorkspaceRole; label: string }[] = [
  { role: "admin", label: "Administrators" },
  { role: "analyst", label: "Analysts" },
  { role: "auditor", label: "Auditors" },
  { role: "manager", label: "Managers" },
];

type Groups = Partial<Record<WorkspaceRole, string[]>>;

function failure(error: unknown): string[] {
  const apiError = asApiError(error);
  return [apiError.message, ...errorReasons(apiError)];
}

function useCsrf(): string {
  const { session } = useAuth();
  return session.status === "signed_in" ? session.user.csrf_token : "";
}

/** One group per line or comma. */
function parseGroups(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\n,]/)
        .map((group) => group.trim())
        .filter(Boolean),
    ),
  ];
}

function groupsFrom(data: FormData): Groups {
  const groups: Groups = {};
  for (const { role } of ROLES) {
    const entry = data.get(role);
    const parsed = typeof entry === "string" ? parseGroups(entry) : [];
    if (parsed.length > 0) groups[role] = parsed;
  }
  return groups;
}

function GroupFields({ value }: { value: Groups | null }) {
  return (
    <div className="d-grid gap-3 cols-sm-2">
      {ROLES.map(({ role, label }) => (
        <label key={role} className="fw-medium">
          {label}: identity provider groups
          <textarea
            name={role}
            rows={2}
            defaultValue={(value?.[role] ?? []).join("\n")}
            placeholder="One group per line"
            className="form-control mt-1"
          />
        </label>
      ))}
    </div>
  );
}

function NewWorkspaceForm({ onCreated }: { onCreated: (message: string) => void }) {
  const csrf = useCsrf();
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const text = (name: string) => {
      const entry = data.get(name);
      return typeof entry === "string" ? entry.trim() : "";
    };
    const body = { slug: text("slug"), name: text("name"), group_roles: groupsFrom(data) };
    const found: string[] = [];
    if (!WORKSPACE_SLUG.test(body.slug)) {
      found.push(
        "The short name uses lowercase letters, digits and dashes, starting with a letter or digit.",
      );
    }
    if (!body.name) found.push("Enter a name.");
    if (Object.keys(body.group_roles).length === 0) {
      found.push("Map at least one identity provider group, or nobody can work in the workspace.");
    }
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const created = await createWorkspace(body, csrf);
      form.reset();
      onCreated(
        `Workspace ${created.name} created. People in its groups can work in it from their next sign-in.`,
      );
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="New workspace"
      noValidate
      className="card card-body mb-3 stack-3"
    >
      <h2 className="fw-semibold">New workspace</h2>
      <Problems messages={problems} />
      <div className="d-grid gap-3 cols-sm-2">
        <label className="fw-medium">
          Name
          <input name="name" maxLength={200} placeholder="Acme" className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          Short name
          <input name="slug" maxLength={63} placeholder="acme" className="form-control mt-1" />
        </label>
      </div>
      <GroupFields value={null} />
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Creating…" : "Create workspace"}
      </button>
    </form>
  );
}

function WorkspaceCard({
  workspace,
  manage,
  onChanged,
}: {
  workspace: WorkspaceInfo;
  manage: boolean;
  onChanged: (message: string) => void;
}) {
  const csrf = useCsrf();
  const [editing, setEditing] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = data.get("name");
    const body: { name?: string; group_roles?: Groups } = {};
    if (typeof name === "string" && name.trim() && name.trim() !== workspace.name) {
      body.name = name.trim();
    }
    if (!workspace.default) body.group_roles = groupsFrom(data);
    setSaving(true);
    setProblems([]);
    try {
      await changeWorkspace(workspace.slug, body, csrf);
      setEditing(false);
      onChanged(`${workspace.name} saved. Group changes apply from each person's next sign-in.`);
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label={workspace.name} className="card card-body mb-3 stack-3">
      <div className="d-flex flex-wrap align-items-center gap-2">
        <h2 className="fw-semibold mb-0">{workspace.name}</h2>
        <code>{workspace.slug}</code>
        {workspace.default && <Badge tone="info">Default</Badge>}
        {workspace.roles.map((role) => (
          <Badge key={role} tone="secondary">{`You: ${role}`}</Badge>
        ))}
      </div>
      <p className="mb-0">
        {workspace.connections.length === 0
          ? "No connections yet. Switch to this workspace and add one on the Connections page."
          : `Connections: ${workspace.connections.map((c) => c.name ?? new URL(c.url).host).join(", ")}`}
      </p>
      {workspace.default ? (
        <p className="small text-secondary mb-0">
          Roles in the default workspace come from the identity provider groups on the{" "}
          <Link to="/settings">Settings page</Link>.
        </p>
      ) : (
        workspace.group_roles && (
          <dl className="mb-0">
            {ROLES.filter(({ role }) => (workspace.group_roles?.[role] ?? []).length > 0).map(
              ({ role, label }) => (
                <div key={role} className="d-flex gap-2">
                  <dt className="fw-medium">{label}:</dt>
                  <dd className="mb-0">{(workspace.group_roles?.[role] ?? []).join(", ")}</dd>
                </div>
              ),
            )}
          </dl>
        )
      )}
      <div className="d-flex flex-wrap gap-2">
        {workspace.roles.length > 0 && (
          <button
            type="button"
            className="btn"
            onClick={() => {
              choose(workspace.slug);
              window.location.assign("/");
            }}
          >
            Work in {workspace.name}
          </button>
        )}
        {manage && !editing && (
          <button type="button" className="btn" onClick={() => setEditing(true)}>
            Edit
          </button>
        )}
      </div>
      {editing && (
        <form
          onSubmit={(event) => void save(event)}
          aria-label={`Edit ${workspace.name}`}
          className="stack-3"
        >
          <Problems messages={problems} />
          <label className="fw-medium">
            Name
            <input
              name="name"
              maxLength={200}
              defaultValue={workspace.name}
              className="form-control mt-1"
            />
          </label>
          {!workspace.default && <GroupFields value={workspace.group_roles} />}
          <div className="d-flex gap-2">
            <button type="submit" disabled={saving} className="btn btn-primary">
              {saving ? "Saving…" : "Save"}
            </button>
            <button type="button" className="btn" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

export function WorkspacesPage() {
  const [message, setMessage] = useState<string | null>(null);
  const loaded = useLoad("workspaces-page", listWorkspaces);
  const manage = loaded.data?.can_manage === true;

  function changed(text: string) {
    setMessage(text);
    loaded.retry();
  }

  return (
    <div className="stack-3">
      <PageHeader
        pretitle="Admin"
        title="Workspaces"
        subtitle="Each workspace keeps its own connections, inventory, baselines, findings, evidence and audit trail. Identity provider groups decide who works in which."
      />
      {message && (
        <Notice tone="success" role="status">
          {message}
        </Notice>
      )}
      {loaded.loading && <Loading label="Loading workspaces…" />}
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {loaded.data?.workspaces.map((workspace) => (
        <WorkspaceCard
          key={workspace.slug}
          workspace={workspace}
          manage={manage}
          onChanged={changed}
        />
      ))}
      {manage && <NewWorkspaceForm onCreated={changed} />}
      {loaded.data && !manage && (
        <p className="small text-secondary">
          Administrators of the default workspace create workspaces and choose their groups.
        </p>
      )}
    </div>
  );
}
