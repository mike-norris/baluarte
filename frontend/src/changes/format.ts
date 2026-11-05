/**
 * Words for change evidence. Every part that was not read reads "Unknown", with the API's reason
 * when it gives one; nothing missing is ever shown as reviewed, passed or deployed.
 */

import {
  changeKinds,
  changeOutcomes,
  type Approvals,
  type Change,
  type ChangeFilters,
  type ChangeKind,
  type ChangeOutcome,
  type ChangePipeline,
  type Deployments,
} from "../api/changes";
import { fromLocal } from "../asof/format";
import { DEPLOYED_TYPE, PIPELINE_TYPE, REVIEWED_TYPE } from "../policies/yaml";
import { formatTime } from "../time";

export const kindLabels: Record<ChangeKind, string> = {
  merge_request: "Merge request",
  direct_push: "Direct push",
  deployment: "Production deployment",
};

export const changeOutcomeLabels: Record<ChangeOutcome, string> = {
  pass: "Meets the rules",
  fail: "Fails a rule",
  unknown: "Unknown",
};

/** A part of the change evidence as a sentence, and whether it is known. */
export interface Part {
  text: string;
  known: boolean;
}

function unknown(reason: string | null | undefined): Part {
  return { text: reason ? `Unknown: ${reason}` : "Unknown", known: false };
}

function plural(n: number, one: string, many: string): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

export function shortSha(sha: string): string {
  return sha.slice(0, 8);
}

/** "!12 Title" for a merge request, "Direct push 1a2b3c4d Title" for a push. */
export function changeName(change: Pick<Change, "kind" | "iid" | "sha" | "title">): string {
  const lead =
    change.kind === "merge_request" && change.iid !== null
      ? `!${String(change.iid)}`
      : `${kindLabels[change.kind]} ${shortSha(change.sha)}`;
  return change.title ? `${lead} ${change.title}` : lead;
}

export function approvalsPart(approvals: Approvals | null | undefined): Part {
  if (!approvals || approvals.status !== "known") return unknown(approvals?.reason);
  const people = approvals.approved_by.map((person) => person.username);
  const parts = [
    people.length === 0
      ? "No approvals"
      : `${plural(people.length, "approval", "approvals")} (${people.join(", ")})`,
  ];
  if (approvals.required !== null) {
    parts.push(`${plural(approvals.required, "approval", "approvals")} required`);
  }
  if (approvals.left !== null && approvals.left > 0) {
    parts.push(`${String(approvals.left)} still needed`);
  }
  if (approvals.self_approved === true) parts.push("approved by its author");
  if (approvals.self_approved === null && people.length > 0) {
    parts.push("whether its author approved is unknown");
  }
  if (approvals.committer_approved === true) parts.push("approved by a committer");
  return { text: parts.join("; "), known: true };
}

export function pipelinePart(pipeline: ChangePipeline | null | undefined): Part {
  if (!pipeline) return unknown(null);
  if (pipeline.status === "unknown") return unknown(pipeline.reason);
  if (pipeline.status === "none") return { text: "No pipeline", known: true };
  const head = `Pipeline ${String(pipeline.id)} ${pipeline.pipeline_status}`;
  const tests = pipeline.tests;
  if (!tests) return { text: `${head}; no test report`, known: true };
  const issues = tests.failed + tests.error;
  return {
    text: `${head}; ${plural(tests.total, "test", "tests")}, ${String(issues)} failed, ${String(tests.skipped)} skipped`,
    known: true,
  };
}

export function deploymentPart(deployments: Deployments | null | undefined): Part {
  if (!deployments || deployments.status !== "known") return unknown(deployments?.reason);
  if (deployments.items.length === 0) {
    return { text: "Not deployed to a watched environment", known: true };
  }
  return {
    text: deployments.items
      .map(
        (item) =>
          `${item.environment}: ${item.status}${item.finished_at ? `, ${formatTime(item.finished_at)}` : ""}`,
      )
      .join("; "),
    known: true,
  };
}

/** The filters as the Changes page's form holds them. */
export interface ChangeForm {
  project: string;
  kind: ChangeKind | "";
  /** ``datetime-local`` values, read as UTC. */
  from: string;
  to: string;
  outcome: ChangeOutcome | "";
}

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return allowed.find((item) => item === value);
}

/** The filters in the URL; invalid values are dropped. */
export function readChangeFilters(params: URLSearchParams): ChangeFilters {
  const filters: ChangeFilters = {};
  const project = Number(params.get("project"));
  if (Number.isSafeInteger(project) && project > 0) filters.project = project;
  const kind = oneOf(params.get("kind"), changeKinds);
  if (kind) filters.kind = kind;
  const outcome = oneOf(params.get("outcome"), changeOutcomes);
  if (outcome) filters.outcome = outcome;
  for (const key of ["from", "to"] as const) {
    const value = fromLocal((params.get(key) ?? "").slice(0, 16));
    if (value) filters[key] = value;
  }
  return filters;
}

export function writeChangeFilters(filters: ChangeFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.project !== undefined) params.set("project", String(filters.project));
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (filters.outcome) params.set("outcome", filters.outcome);
  return params;
}

export function hasChangeFilters(filters: ChangeFilters): boolean {
  return writeChangeFilters(filters).toString() !== "";
}

/** The form's fields; empty and invalid values are left out. */
export function formFilters(form: ChangeForm): ChangeFilters {
  const query = new URLSearchParams();
  if (form.project.trim()) query.set("project", form.project.trim());
  if (form.kind) query.set("kind", form.kind);
  if (form.from) query.set("from", form.from);
  if (form.to) query.set("to", form.to);
  if (form.outcome) query.set("outcome", form.outcome);
  return readChangeFilters(query);
}

export const changeRuleTypes: readonly string[] = [REVIEWED_TYPE, PIPELINE_TYPE, DEPLOYED_TYPE];

export function isChangeRule(ruleType: string): boolean {
  return changeRuleTypes.includes(ruleType);
}

export interface FailingChange {
  id: number;
  kind: ChangeKind;
  iid: number | null;
  sha: string;
  title: string | null;
}

function isFailing(value: unknown): value is FailingChange {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.id === "number" &&
    (item.kind === "merge_request" || item.kind === "direct_push" || item.kind === "deployment") &&
    typeof item.sha === "string"
  );
}

/** The changes a change rule failed on, from its observed value, read defensively. */
export function failingChanges(observed: unknown): { items: FailingChange[]; total: number } {
  if (typeof observed !== "object" || observed === null) return { items: [], total: 0 };
  const record = observed as Record<string, unknown>;
  const items = Array.isArray(record.failing)
    ? record.failing.filter(isFailing).map((item) => ({
        ...item,
        iid: typeof item.iid === "number" ? item.iid : null,
        title: typeof item.title === "string" ? item.title : null,
      }))
    : [];
  const total =
    typeof record.failing_total === "number"
      ? Math.max(record.failing_total, items.length)
      : items.length;
  return { items, total };
}
