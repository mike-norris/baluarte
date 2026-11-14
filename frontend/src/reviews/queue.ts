import type { ApiError } from "../api/client";
import { controlPath, listMappingChanges } from "../api/controls";
import { listControlExceptions } from "../api/compliance";
import { listExceptions } from "../api/exceptions";
import { issuePlanHref, listIssuePlans } from "../api/issuePlans";
import { getTaxonomy, listLabelPlans } from "../api/labels";
import { listPolicyChanges, type Person } from "../api/policies";
import { listPlans } from "../api/remediation";
import { listExclusions } from "../api/scope";
import type { CurrentUser } from "../auth/session";
import { CHANGES_VIEW } from "../policies/format";

/** The kinds of item that wait for a second person's decision. */
export type ReviewKind =
  | "baseline_change"
  | "taxonomy"
  | "label_plan"
  | "issue_plan"
  | "remediation_plan"
  | "exception"
  | "scope_exclusion"
  | "mapping_change"
  | "control_exception";

export const kindLabels: Record<ReviewKind, string> = {
  baseline_change: "Baseline change",
  taxonomy: "Label taxonomy",
  label_plan: "Label plan",
  issue_plan: "Issue time plan",
  remediation_plan: "Remediation plan",
  exception: "Exception",
  scope_exclusion: "Monitoring exclusion",
  mapping_change: "Control mapping change",
  control_exception: "Compliance exception",
};

/** Who may decide each kind; the API checks this and separation of duties regardless. */
export const deciderPermission: Record<ReviewKind, string> = {
  baseline_change: "policy:write",
  taxonomy: "policy:write",
  label_plan: "remediation:approve",
  issue_plan: "remediation:approve",
  remediation_plan: "remediation:approve",
  exception: "exception:approve",
  scope_exclusion: "policy:write",
  mapping_change: "policy:write",
  control_exception: "settings:manage",
};

export interface ReviewItem {
  kind: ReviewKind;
  id: number;
  title: string;
  href: string;
  proposedBy: string;
  /** The proposer's user ID where the API gives one. */
  proposerId: number | null;
  proposedAt: string;
  expiresAt: string | null;
}

export interface ReviewQueue {
  items: ReviewItem[];
  /** Kinds whose list could not be read, so the queue may be missing items of that kind. */
  unreadable: ReviewKind[];
  /** Kinds with more pending items than one page; counts are then lower bounds. */
  partial: ReviewKind[];
}

function who(person: Person | number | null | undefined): { name: string; id: number | null } {
  if (person === null || person === undefined) return { name: "Unknown", id: null };
  if (typeof person === "number") return { name: `User ${String(person)}`, id: person };
  return {
    name: person.display_name ?? (person.user_id ? `User ${String(person.user_id)}` : person.actor),
    id: person.user_id,
  };
}

interface Source {
  kind: ReviewKind;
  read: (signal: AbortSignal) => Promise<{ items: ReviewItem[]; more: boolean }>;
}

const sources: readonly Source[] = [
  {
    kind: "baseline_change",
    read: async (signal) => {
      const page = await listPolicyChanges({ state: "proposed" }, signal);
      return {
        more: page.next_before_id !== null,
        items: page.items.map((change) => {
          const person = who(change.proposed_by);
          return {
            kind: "baseline_change",
            id: change.id,
            title: `${change.policy_name}${change.target_path ? ` on ${change.target_path}` : ""}`,
            href: CHANGES_VIEW,
            proposedBy: person.name,
            proposerId: person.id,
            proposedAt: change.proposed_at,
            expiresAt: null,
          };
        }),
      };
    },
  },
  {
    kind: "taxonomy",
    read: async (signal) => {
      const view = await getTaxonomy(null, signal);
      return {
        more: false,
        items: view.versions
          .filter((version) => version.state === "proposed")
          .map((version) => {
            const person = who(version.proposed_by);
            return {
              kind: "taxonomy",
              id: version.id,
              title: `${version.name} v${String(version.version)}`,
              href: "/labels",
              proposedBy: person.name,
              proposerId: person.id,
              proposedAt: version.proposed_at,
              expiresAt: null,
            };
          }),
      };
    },
  },
  {
    kind: "label_plan",
    read: async (signal) => {
      const page = await listLabelPlans("submitted", null, signal);
      return {
        more: page.next_after_id !== null,
        items: page.items.map((plan) => {
          const person = who(plan.created_by);
          return {
            kind: "label_plan",
            id: plan.id,
            title: `Label plan ${String(plan.id)}: ${String(plan.steps)} steps`,
            href: `/labels/plans/${String(plan.id)}`,
            proposedBy: person.name,
            proposerId: person.id,
            proposedAt: plan.created_at,
            expiresAt: plan.expires_at,
          };
        }),
      };
    },
  },
  {
    kind: "issue_plan",
    read: async (signal) => {
      const page = await listIssuePlans("submitted", null, signal);
      return {
        more: page.next_after_id !== null,
        items: page.items.map((plan) => {
          const person = who(plan.created_by);
          return {
            kind: "issue_plan",
            id: plan.id,
            title: `Issue plan ${String(plan.id)}: ${String(plan.steps)} steps`,
            href: issuePlanHref(plan.id),
            proposedBy: person.name,
            proposerId: person.id,
            proposedAt: plan.created_at,
            expiresAt: plan.expires_at,
          };
        }),
      };
    },
  },
  {
    kind: "remediation_plan",
    read: async (signal) => {
      const page = await listPlans("submitted", null, signal);
      return {
        more: page.next_after_id !== null,
        items: page.items.map((plan) => ({
          kind: "remediation_plan",
          id: plan.id,
          title: `Remediation plan ${String(plan.id)}: ${String(plan.steps)} steps`,
          href: `/remediation/${String(plan.id)}`,
          proposedBy: `User ${String(plan.created_by_user_id)}`,
          proposerId: plan.created_by_user_id,
          proposedAt: plan.submitted_at ?? plan.created_at,
          expiresAt: plan.expires_at,
        })),
      };
    },
  },
  {
    kind: "exception",
    read: async (signal) => {
      const page = await listExceptions({ state: "requested" }, null, signal);
      return {
        more: page.next_after_id !== null,
        items: page.items.map((exception) => ({
          kind: "exception",
          id: exception.id,
          title: `${exception.policy_name} ${exception.rule_id} on ${exception.project.path}`,
          href: `/exceptions/${String(exception.id)}`,
          proposedBy:
            exception.requested_by.display_name ??
            `User ${String(exception.requested_by.user_id)}`,
          proposerId: exception.requested_by.user_id,
          proposedAt: exception.requested_at,
          expiresAt: null,
        })),
      };
    },
  },
  {
    kind: "scope_exclusion",
    read: async (signal) => {
      const list = await listExclusions("proposed", signal);
      return {
        more: false,
        items: list.items.map((exclusion) => {
          const person = who(exclusion.proposed_by);
          return {
            kind: "scope_exclusion",
            id: exclusion.id,
            title: `Stop monitoring ${exclusion.target_path ?? exclusion.target_type}`,
            href: "/inventory",
            proposedBy: person.name,
            proposerId: person.id,
            proposedAt: exclusion.proposed_at,
            expiresAt: null,
          };
        }),
      };
    },
  },
  {
    kind: "mapping_change",
    read: async (signal) => {
      const list = await listMappingChanges("proposed", signal);
      return {
        more: false,
        items: list.items.map((change) => {
          const person = who(change.proposed_by);
          return {
            kind: "mapping_change",
            id: change.id,
            title: `${change.framework.toUpperCase()} ${change.control_id}`,
            href: controlPath(change.framework, change.control_id),
            proposedBy: person.name,
            proposerId: person.id,
            proposedAt: change.proposed_at,
            expiresAt: null,
          };
        }),
      };
    },
  },
  {
    kind: "control_exception",
    read: async (signal) => {
      const items = await listControlExceptions(signal);
      return {
        more: false,
        items: items
          .filter((exception) => exception.state === "proposed")
          .map((exception) => ({
            kind: "control_exception",
            id: exception.id,
            title: `Control ${exception.control_id}`,
            href: "/compliance",
            proposedBy: exception.requested_by,
            proposerId: null,
            proposedAt: exception.requested_at,
            expiresAt: null,
          })),
      };
    },
  },
];

/** Sources this user can read: deciders, plus what they could have proposed themselves. */
function readable(user: CurrentUser, kind: ReviewKind): boolean {
  if (kind === "control_exception") return user.permissions.includes("compliance:read");
  return true;
}

/**
 * Every item waiting for a decision, read from each kind's own list (one page each). The queue
 * says which kinds could not be read or have more than one page, rather than undercounting quietly.
 */
export async function loadReviewQueue(
  user: CurrentUser,
  signal: AbortSignal,
): Promise<ReviewQueue> {
  const wanted = sources.filter((source) => readable(user, source.kind));
  const results = await Promise.allSettled(wanted.map((source) => source.read(signal)));
  const queue: ReviewQueue = { items: [], unreadable: [], partial: [] };
  results.forEach((result, index) => {
    const { kind } = wanted[index] as Source;
    if (result.status === "rejected") {
      const status = (result.reason as ApiError | undefined)?.status;
      // A list the user may not read holds nothing they could decide or have proposed.
      if (status !== 403) queue.unreadable.push(kind);
      return;
    }
    queue.items.push(...result.value.items);
    if (result.value.more) queue.partial.push(kind);
  });
  queue.items.sort(
    (a, b) =>
      (a.expiresAt ?? "9999").localeCompare(b.expiresAt ?? "9999") ||
      a.proposedAt.localeCompare(b.proposedAt),
  );
  return queue;
}

/** Whether this user may decide the item: the permission, and not their own outside dev mode. */
export function canDecide(user: CurrentUser, item: ReviewItem): boolean {
  if (!user.permissions.includes(deciderPermission[item.kind])) return false;
  return item.proposerId !== user.user_id || user.development_mode === true;
}

export function isMine(user: CurrentUser, item: ReviewItem): boolean {
  return item.proposerId !== null && item.proposerId === user.user_id;
}
