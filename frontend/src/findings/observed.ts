/**
 * One-line summaries of the observed values of the access-control and project approval rules.
 * Their payloads are lists (members, tokens, approval rules, groups) that read poorly as JSON in
 * a table, so each gets a sentence; the full value is still shown as JSON where there is room.
 * Payloads are read defensively: a summary is null when the shape is not recognised, and the
 * caller falls back to the JSON.
 */

type Payload = Record<string, unknown>;

function isRecord(value: unknown): value is Payload {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function records(value: unknown): Payload[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function count(n: number, one: string, many: string): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

function whole(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function approvals(observed: Payload): string | null {
  const rules = records(observed.rules);
  const settings = isRecord(observed.settings) ? observed.settings : observed;
  const parts: string[] = [];
  const required = rules
    .map((rule) => whole(rule.approvals_required))
    .filter((n): n is number => n !== null);
  parts.push(
    rules.length === 0
      ? "No approval rules"
      : `${count(rules.length, "approval rule", "approval rules")}, requiring at most ${count(Math.max(0, ...required), "approval", "approvals")}`,
  );
  const flags: [string, boolean, string, string][] = [
    [
      "merge_requests_author_approval",
      false,
      "authors can't approve",
      "authors can approve their own",
    ],
    [
      "merge_requests_disable_committers_approval",
      true,
      "committers can't approve",
      "committers can approve",
    ],
    [
      "reset_approvals_on_push",
      true,
      "a push removes approvals",
      "approvals survive a push",
    ],
    [
      "disable_overriding_approvers_per_merge_request",
      true,
      "merge requests can't change their rules",
      "merge requests can change their rules",
    ],
  ];
  let known = 0;
  for (const [key, strict, strictText, looseText] of flags) {
    const value = settings[key];
    if (typeof value === "boolean") {
      parts.push(value === strict ? strictText : looseText);
      known += 1;
    }
  }
  if (!Array.isArray(observed.rules) && known === 0) return null;
  return `${parts.join("; ")}.`;
}

function members(observed: Payload): string | null {
  const seen = new Map<string, Payload>();
  for (const value of Object.values(observed)) {
    for (const member of records(value)) {
      if (typeof member.username === "string") seen.set(member.username, member);
    }
  }
  const listed = [...seen.values()];
  const privileged = listed.filter((member) => (whole(member.access_level) ?? 0) >= 40);
  const inactive = listed.filter(
    (member) => typeof member.state === "string" && member.state !== "active",
  );
  const privilegedCount =
    whole(observed.privileged_count) ?? (listed.length > 0 ? privileged.length : null);
  if (privilegedCount === null) return null;
  const names = privileged
    .map((member) => String(member.username))
    .sort()
    .slice(0, 10);
  const more = privileged.length > names.length ? ", …" : "";
  const parts = [
    `${count(privilegedCount, "member", "members")} with Maintainer or Owner access${names.length > 0 ? ` (${names.join(", ")}${more})` : ""}`,
  ];
  if (inactive.length > 0) {
    parts.push(
      `${count(inactive.length, "account", "accounts")} not active: ${inactive
        .map((member) => `${String(member.username)} (${String(member.state)})`)
        .join(", ")}`,
    );
  }
  return `${parts.join("; ")}.`;
}

const DAY = 24 * 60 * 60 * 1000;

function days(from: unknown, to: unknown): number | null {
  if (typeof from !== "string" || typeof to !== "string") return null;
  const start = Date.parse(from);
  const end = Date.parse(to);
  return Number.isNaN(start) || Number.isNaN(end) ? null : Math.round((end - start) / DAY);
}

function tokens(observed: unknown): string | null {
  const list = Array.isArray(observed)
    ? records(observed)
    : isRecord(observed)
      ? records(observed.tokens)
      : null;
  if (list === null) return null;
  // The backend lists only active tokens; older or other shapes may carry the flags.
  const active = list.filter((token) => token.active !== false && token.revoked !== true);
  if (active.length === 0) return "No active project access tokens.";
  const parts = [count(active.length, "active token", "active tokens")];
  const noExpiry = active.filter((token) => typeof token.expires_at !== "string");
  if (noExpiry.length > 0) parts.push(`${String(noExpiry.length)} without an expiry date`);
  const lifetimes = active
    .map((token) => days(token.created_on ?? token.created_at, token.expires_at))
    .filter((n): n is number => n !== null);
  if (lifetimes.length > 0) {
    parts.push(`longest lifetime ${count(Math.max(...lifetimes), "day", "days")}`);
  }
  const unused = active.filter(
    (token) => typeof (token.last_used_on ?? token.last_used_at) !== "string",
  );
  if (unused.length > 0) parts.push(`${String(unused.length)} never used`);
  return `${parts.join("; ")}.`;
}

function twoFactor(observed: Payload): string | null {
  const groups = records(observed.groups);
  const enforcedBy = observed.enforced_by;
  if (enforcedBy === "instance") {
    const instance = isRecord(observed.instance) ? observed.instance : {};
    const grace = whole(instance.two_factor_grace_period);
    return `Required by the instance setting${grace === null ? "" : `, grace period ${count(grace, "hour", "hours")}`}.`;
  }
  if (typeof enforcedBy === "string" && enforcedBy) {
    const group = groups.find((item) => item.path === enforcedBy);
    const grace = whole(group?.two_factor_grace_period);
    return `Required by ${enforcedBy}${grace === null ? "" : `, grace period ${count(grace, "hour", "hours")}`}.`;
  }
  if (groups.length === 0 && !isRecord(observed.instance)) return null;
  const instance = isRecord(observed.instance)
    ? observed.instance.require_two_factor_authentication
    : undefined;
  const instanceText =
    instance === true
      ? " The instance requires it."
      : instance === false
        ? " Nor does the instance."
        : "";
  return `No group above the project requires two-factor authentication (${count(groups.length, "group", "groups")} checked).${instanceText}`;
}

/** A sentence for a rule type's observed value, or null to show the value as it is. */
/** Change rules count the changes in their window; the failing ones are listed separately. */
function changes(observed: Payload): string | null {
  const total = whole(observed.changes);
  if (total === null) return null;
  if (total === 0) return "No changes in the window";
  const parts = (["passed", "failed", "unknown"] as const).map(
    (key) => `${String(whole(observed[key]) ?? 0)} ${key}`,
  );
  return `${count(total, "change", "changes")} in the window: ${parts.join(", ")}`;
}

export function observedSummary(ruleType: string, observed: unknown): string | null {
  if (ruleType === "gitlab.project.access_tokens") return tokens(observed);
  if (!isRecord(observed)) return null;
  switch (ruleType) {
    case "gitlab.project.merge_request_approvals":
      return approvals(observed);
    case "gitlab.project.privileged_members":
      return members(observed);
    case "gitlab.group.two_factor_required":
      return twoFactor(observed);
    case "gitlab.change.reviewed":
    case "gitlab.change.pipeline_passed":
    case "gitlab.change.deployed_after_review":
      return changes(observed);
    default:
      return null;
  }
}
