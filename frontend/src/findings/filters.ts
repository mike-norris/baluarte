import {
  isSeverity,
  outcomes,
  type FindingFilters,
  type FindingState,
  type Outcome,
} from "../api/evaluations";
import { parseTagParam, tagParam } from "../api/tags";

const states: readonly FindingState[] = ["open", "resolved", "excepted"];
// The API limits policy names and rule IDs to 63 characters.
const MAX_NAME = 63;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return allowed.find((item) => item === value);
}

// Rule types are dotted lowercase names, e.g. gitlab.labels.consistent.
const RULE_TYPE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/;

function name(value: string | null): string | undefined {
  const text = value?.trim();
  return text && text.length <= MAX_NAME ? text : undefined;
}

/**
 * Findings filters from the URL, in the API's parameter names, so the dashboard's links and
 * shared URLs open the same view. Invalid values are dropped rather than sent.
 */
export function readFindingFilters(params: URLSearchParams): FindingFilters {
  const filters: FindingFilters = {};
  const state = oneOf(params.get("state"), states);
  if (state) filters.state = state;
  const outcome = oneOf<Outcome>(params.get("outcome"), outcomes);
  if (outcome) filters.outcome = outcome;
  const severity = params.get("severity");
  if (severity && isSeverity(severity)) filters.severity = severity;
  const project = Number(params.get("project"));
  if (Number.isSafeInteger(project) && project > 0) filters.project = project;
  const policy = name(params.get("policy"));
  if (policy) filters.policy = policy;
  const rule = name(params.get("rule"));
  if (rule) filters.rule = rule;
  const ruleType = params.get("rule_type")?.trim();
  if (ruleType && ruleType.length <= 255 && RULE_TYPE.test(ruleType)) filters.rule_type = ruleType;
  const tag = parseTagParam(params.get("tag"));
  if (tag) filters.tag = tagParam(tag);
  return filters;
}

/** The URL query for ``filters``, in a fixed order. */
export function writeFindingFilters(filters: FindingFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.outcome) params.set("outcome", filters.outcome);
  if (filters.severity) params.set("severity", filters.severity);
  if (filters.project !== undefined) params.set("project", String(filters.project));
  if (filters.policy) params.set("policy", filters.policy);
  if (filters.rule) params.set("rule", filters.rule);
  if (filters.rule_type) params.set("rule_type", filters.rule_type);
  if (filters.tag) params.set("tag", filters.tag);
  return params;
}

export function hasFindingFilters(filters: FindingFilters): boolean {
  return writeFindingFilters(filters).size > 0;
}
