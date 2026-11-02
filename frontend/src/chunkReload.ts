/**
 * Recovering from a page bundle that no longer exists.
 *
 * Page bundles have content-hashed names. A tab opened before Baluarte was upgraded still holds
 * the old app, so opening a page it has not loaded yet asks for a bundle the new deployment no
 * longer serves. One full reload fetches the current app; a timestamp in sessionStorage stops a
 * genuinely broken bundle from reloading in a loop, and the error then reaches the route's error
 * page instead.
 */

const KEY = "baluarte:chunk-reload-at";
/** A second failure within this window is not a stale tab, so it is shown, not reloaded. */
export const RELOAD_WINDOW_MS = 30_000;

/** Browsers word this differently: Chrome, Firefox and Safari respectively. */
const PATTERNS = [
  /Failed to fetch dynamically imported module/i,
  /error loading dynamically imported module/i,
  /Importing a module script failed/i,
];

export function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return PATTERNS.some((pattern) => pattern.test(message));
}

function lastReload(): number | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw === null ? null : Number(raw);
  } catch {
    return null;
  }
}

function markReload(now: number): boolean {
  try {
    sessionStorage.setItem(KEY, String(now));
    return true;
  } catch {
    // Without storage there is no loop guard, so do not reload automatically.
    return false;
  }
}

/**
 * Reloads the page once for a stale bundle. Returns true when a reload was started; the caller
 * then waits rather than rendering an error.
 */
export function reloadForStaleChunk(
  error: unknown,
  now: number = Date.now(),
  reload: () => void = () => window.location.reload(),
): boolean {
  if (!isChunkLoadError(error)) return false;
  const last = lastReload();
  if (last !== null && now - last < RELOAD_WINDOW_MS) return false;
  if (!markReload(now)) return false;
  reload();
  return true;
}

/** Wraps a dynamic import so a stale bundle reloads the app instead of failing the route. */
export function withStaleChunkReload<T>(load: () => Promise<T>): () => Promise<T> {
  return async () => {
    try {
      return await load();
    } catch (error) {
      if (reloadForStaleChunk(error)) {
        // The page is going away; never settle, so nothing renders an error meanwhile.
        return new Promise<T>(() => undefined);
      }
      throw error;
    }
  };
}
