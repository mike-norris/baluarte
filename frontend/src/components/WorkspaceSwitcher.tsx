import { useLoad } from "../api/useLoad";
import {
  choose,
  chosenConnection,
  listWorkspaces,
  type ConnectionChoice,
  type WorkspaceInfo,
} from "../api/workspace";
import { useAuth } from "../auth/context";

function label(connection: ConnectionChoice): string {
  return connection.name ?? new URL(connection.url).host;
}

/** The connection a workspace shows when none is chosen: its primary one, or its oldest. */
export function defaultConnection(workspace: WorkspaceInfo): ConnectionChoice | undefined {
  return workspace.connections.find((c) => c.primary) ?? workspace.connections[0];
}

/**
 * Chooses the workspace and connection to work in. Shown only when there is a choice: more than
 * one workspace, or more than one connection. Switching starts again from the dashboard, since
 * records differ between workspaces.
 */
export function WorkspaceSwitcher() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const loaded = useLoad(`workspaces:${user?.workspace ?? ""}`, listWorkspaces);
  const mine = (loaded.data?.workspaces ?? []).filter((w) => w.roles.length > 0);
  const choices = mine.reduce((count, w) => count + Math.max(w.connections.length, 1), 0);
  if (!user || choices < 2) return null;

  const current = mine.find((w) => w.slug === user.workspace) ?? mine[0];
  const chosen = chosenConnection();
  const connection =
    current?.connections.find((c) => c.id === chosen) ??
    (current ? defaultConnection(current) : undefined);
  const value = `${current?.slug ?? ""}:${connection ? String(connection.id) : ""}`;

  function change(next: string) {
    const [slug = "", id = ""] = next.split(":");
    choose(slug, id ? Number(id) : null);
    window.location.assign("/");
  }

  return (
    <label className="d-flex align-items-center gap-2 mb-0">
      <span className="visually-hidden">Workspace and connection</span>
      <select
        className="form-select form-select-sm"
        value={value}
        onChange={(event) => change(event.target.value)}
      >
        {mine.map((workspace) => (
          <optgroup key={workspace.slug} label={workspace.name}>
            {workspace.connections.length === 0 ? (
              <option value={`${workspace.slug}:`}>{workspace.name}: no connections yet</option>
            ) : (
              workspace.connections.map((c) => (
                <option key={c.id} value={`${workspace.slug}:${String(c.id)}`}>
                  {mine.length > 1 ? `${workspace.name}: ${label(c)}` : label(c)}
                </option>
              ))
            )}
          </optgroup>
        ))}
      </select>
    </label>
  );
}
