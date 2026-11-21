import { getJson } from "../api/client";
import { controlPath } from "../api/controls";
import { issuePlanHref } from "../api/issuePlans";
import { issueHref } from "../api/register";
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
  | "control_exception"
  | "issue_mapping";

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
  issue_mapping: "Issue-to-control mapping",
};

export interface ReviewItem {
  kind: ReviewKind;
  id: number;
  title: string;
  href: string;
  proposedBy: string;
  /** The proposer's user ID where it is known. */
  proposerId: number | null;
  proposedAt: string;
  expiresAt: string | null;
  /** Whether this user may decide it: the API applies the permission and separation of duties. */
  canDecide: boolean;
}

export interface ReviewQueue {
  items: ReviewItem[];
  /** Kinds with more waiting items than one page; counts are then lower bounds. */
  partial: ReviewKind[];
}

interface ReviewItemOut {
  kind: ReviewKind;
  id: number;
  title: string;
  proposed_by: { user_id: number | null; display_name: string | null };
  proposed_at: string;
  expires_at: string | null;
  can_decide: boolean;
  framework: string | null;
  control_id: string | null;
  project_id?: number | null;
  iid?: number | null;
}

function href(item: ReviewItemOut): string {
  const id = String(item.id);
  switch (item.kind) {
    case "baseline_change":
      return CHANGES_VIEW;
    case "taxonomy":
      return "/labels";
    case "label_plan":
      return `/labels/plans/${id}`;
    case "issue_plan":
      return issuePlanHref(item.id);
    case "remediation_plan":
      return `/remediation/${id}`;
    case "exception":
      return `/exceptions/${id}`;
    case "scope_exclusion":
      // Exclusions are decided on the changes page, beside baseline and mapping changes.
      return CHANGES_VIEW;
    case "mapping_change":
      return item.framework && item.control_id
        ? controlPath(item.framework, item.control_id)
        : "/controls";
    case "control_exception":
      return "/compliance";
    case "issue_mapping":
      return item.project_id && item.iid
        ? issueHref(item.project_id, item.iid)
        : "/register/controls";
  }
}

/**
 * Everything waiting for a decision that this user may see, in one request: soonest to expire
 * first, then oldest. The API says which kinds have more than it returned.
 */
export async function loadReviewQueue(signal: AbortSignal): Promise<ReviewQueue> {
  const queue = await getJson<{ items: ReviewItemOut[]; partial: ReviewKind[] }>(
    "/api/v1/reviews",
    undefined,
    signal,
  );
  return {
    partial: queue.partial,
    items: queue.items.map((item) => ({
      kind: item.kind,
      id: item.id,
      title: item.title,
      href: href(item),
      proposedBy:
        item.proposed_by.display_name ??
        (item.proposed_by.user_id === null
          ? "Unknown"
          : `User ${String(item.proposed_by.user_id)}`),
      proposerId: item.proposed_by.user_id,
      proposedAt: item.proposed_at,
      expiresAt: item.expires_at,
      canDecide: item.can_decide,
    })),
  };
}

export function isMine(user: CurrentUser, item: ReviewItem): boolean {
  return item.proposerId !== null && item.proposerId === user.user_id;
}
