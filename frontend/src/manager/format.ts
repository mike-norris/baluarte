/** What the manager space shows to whom, and its URL state. The API enforces access regardless. */

import type { Focus } from "../api/manager";
import { parseTagParam, tagParam } from "../api/tags";
import type { CurrentUser } from "../auth/session";

export const MANAGER = "/manager";
export const MANAGER_TRENDS = "/manager/trends";
export const MANAGER_TIME = "/manager/time";
export const MANAGER_TAGS = "/manager/tags";

function has(user: CurrentUser | null, permission: string): boolean {
  return user?.permissions.includes(permission) ?? false;
}

export const canReadManager = (user: CurrentUser | null) =>
  has(user, "manager:read");
export const canReadTime = (user: CurrentUser | null) => has(user, "time:read");
export const canReadPeople = (user: CurrentUser | null) =>
  has(user, "time:read_people");
export const canWriteTags = (user: CurrentUser | null) =>
  has(user, "tags:write");

export interface ManagerTab {
  path: string;
  label: string;
  visible: (user: CurrentUser | null) => boolean;
}

export const managerTabs: readonly ManagerTab[] = [
  { path: MANAGER, label: "Overview", visible: canReadManager },
  { path: MANAGER_TRENDS, label: "Trends", visible: canReadManager },
  { path: MANAGER_TIME, label: "Time", visible: canReadTime },
  { path: MANAGER_TAGS, label: "Tags", visible: () => true },
];

// GitLab group paths: segments of letters, digits, dot, dash and underscore, not starting with
// a dot or dash.
const SEGMENT = "[A-Za-z0-9_][A-Za-z0-9_.-]*";
const GROUP_PATH = new RegExp(`^${SEGMENT}(/${SEGMENT})*$`);

/** The group or tag the overview and trends are rolled up to, from the URL. */
export function readFocus(params: URLSearchParams): Focus {
  const group = params.get("group")?.trim();
  if (group && group.length <= 255 && GROUP_PATH.test(group)) return { group };
  const tag = parseTagParam(params.get("tag"));
  return tag ? { tag: tagParam(tag) } : {};
}

export function writeFocus(
  focus: Focus,
  params = new URLSearchParams(),
): URLSearchParams {
  const next = new URLSearchParams(params);
  next.delete("group");
  next.delete("tag");
  if (focus.group) next.set("group", focus.group);
  else if (focus.tag) next.set("tag", focus.tag);
  return next;
}

/** The focus in words. */
export function focusText(focus: Focus): string {
  if (focus.group) return `group ${focus.group}`;
  if (focus.tag) return `tag ${focus.tag.replace(":", "=")}`;
  return "all top-level groups";
}

/** The parent of a group path, or null for a top-level group. */
export function parentPath(path: string): string | null {
  const index = path.lastIndexOf("/");
  return index > 0 ? path.slice(0, index) : null;
}

/** Words for the figures the API shows as of now while the page shows the past. */
const currentOnlyWords: Record<string, string> = {
  time: "time tracking",
  tags: "tags",
  labels: "label drift",
  label_drift: "label drift",
  projects: "project counts",
};

export function currentOnlyText(names: readonly string[]): string | null {
  if (names.length === 0) return null;
  const words = names.map(
    (name) => currentOnlyWords[name] ?? name.replace(/_/g, " "),
  );
  return `Shown as of now, because Baluarte keeps no history of them: ${words.join(", ")}.`;
}
