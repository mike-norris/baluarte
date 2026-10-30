import {
  MAX_EXCEPTION_DAYS,
  exceptionStates,
  type ExceptionFilters,
  type ExceptionState,
  type PolicyException,
} from "../api/exceptions";
import type { CurrentUser } from "../auth/session";

export const exceptionStateLabels: Record<ExceptionState, string> = {
  requested: "Requested",
  approved: "Approved",
  rejected: "Rejected",
  revoked: "Revoked",
  expired: "Expired",
};

export const exceptionStateMeaning: Record<ExceptionState, string> = {
  requested: "Waiting for an approver. Nothing is waived yet.",
  approved: "In force: failures of this rule for this project are recorded as excepted.",
  rejected: "Refused by an approver. Nothing was waived.",
  revoked: "Ended early by an approver. The finding is open again if the rule still fails.",
  expired: "Past its expiry. The finding is open again if the rule still fails.",
};

export const CAN_REQUEST = "exception:request";
export const CAN_APPROVE = "exception:approve";

export function hasPermission(user: CurrentUser, permission: string): boolean {
  return user.permissions.includes(permission);
}

/**
 * Whether the signed-in user requested this exception, by internal user ID. The API enforces
 * separation of duties either way; this only hides actions it would refuse.
 */
export function isOwn(exception: PolicyException, user: CurrentUser): boolean {
  return exception.requested_by.user_id === user.user_id;
}

export function requesterName(exception: PolicyException): string {
  return exception.requested_by.display_name ?? `User ${String(exception.requested_by.user_id)}`;
}

function utcDate(time: number): string {
  return new Date(time).toISOString().slice(0, 10);
}

const DAY = 24 * 60 * 60 * 1000;

/**
 * The expiry dates the form offers, as UTC ``YYYY-MM-DD``. An exception ends at the end of the
 * chosen UTC day, so the last offered day keeps it within ``MAX_EXCEPTION_DAYS`` of now.
 */
export function expiryRange(now = Date.now()): { min: string; max: string } {
  return { min: utcDate(now), max: utcDate(now + (MAX_EXCEPTION_DAYS - 1) * DAY) };
}

/** The end of a UTC day, as the API's ``expires_at``. */
export function endOfUtcDay(date: string): string {
  return `${date}T23:59:59Z`;
}

/** Exceptions filters from the URL; invalid values are dropped rather than sent. */
export function readExceptionFilters(params: URLSearchParams): ExceptionFilters {
  const filters: ExceptionFilters = {};
  const state = exceptionStates.find((item) => item === params.get("state"));
  if (state) filters.state = state;
  const project = Number(params.get("project"));
  if (Number.isSafeInteger(project) && project > 0) filters.project = project;
  for (const field of ["policy", "rule"] as const) {
    const text = params.get(field)?.trim();
    if (text && text.length <= 63) filters[field] = text;
  }
  return filters;
}

export function writeExceptionFilters(filters: ExceptionFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.project !== undefined) params.set("project", String(filters.project));
  if (filters.policy) params.set("policy", filters.policy);
  if (filters.rule) params.set("rule", filters.rule);
  return params;
}
