/**
 * The workspace and connection this browser works in (roadmap M11.4).
 *
 * Kept in local storage and sent with every API request as ``X-Baluarte-Workspace`` and
 * ``X-Baluarte-Connection``; without them the API uses the person's default workspace and its
 * primary connection. The server checks both on every request. No imports, so every API module
 * can use it.
 */

const WORKSPACE_KEY = "baluarte.workspace";
const CONNECTION_KEY = "baluarte.connection";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Storage is unavailable: the choice lasts until the page is reloaded.
  }
}

export function chosenWorkspace(): string | null {
  return read(WORKSPACE_KEY);
}

export function chosenConnection(): number | null {
  const value = read(CONNECTION_KEY);
  return value !== null && /^\d+$/.test(value) ? Number(value) : null;
}

/** Choose where to work; a null connection means the workspace's default one. */
export function choose(workspace: string | null, connection: number | null = null): void {
  write(WORKSPACE_KEY, workspace);
  write(CONNECTION_KEY, connection === null ? null : String(connection));
}

/** The headers that put a request in the chosen workspace and connection. */
export function scopeHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const workspace = chosenWorkspace();
  const connection = chosenConnection();
  if (workspace) headers["X-Baluarte-Workspace"] = workspace;
  if (connection !== null) headers["X-Baluarte-Connection"] = String(connection);
  return headers;
}
