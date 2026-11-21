/**
 * Client for the auditable issue register (roadmap M12): issues a selection rule matches or
 * someone included, why, and in which category. See docs/ISSUE_REGISTER.md.
 */

import { getJson } from "./client";
import { sendJson } from "./send";

export type RegisterCategory = "engineering" | "security_finding" | "exception" | "remediation";

export const categoryLabels: Record<RegisterCategory, string> = {
  engineering: "Engineering",
  security_finding: "Security finding",
  exception: "Exception",
  remediation: "Remediation",
};

export const categories = Object.keys(categoryLabels) as RegisterCategory[];

export type IssueType = "issue" | "incident" | "task" | "test_case" | "requirement" | "ticket";

export const issueTypeLabels: Record<IssueType, string> = {
  issue: "Issue",
  incident: "Incident",
  task: "Task",
  test_case: "Test case",
  requirement: "Requirement",
  ticket: "Ticket",
};

export interface Reason {
  kind: "rule" | "included" | "excluded";
  rule_id: number | null;
  rule_name: string | null;
  decision_id: number | null;
  reason: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface RegisterIssue {
  project_id: number;
  project: string;
  connection_id: number;
  iid: number;
  issue_type: string | null;
  title: string | null;
  state: string | null;
  labels: string[];
  author: string | null;
  assignees: string[];
  milestone: string | null;
  web_url: string | null;
  confidential: boolean;
  opened_at: string | null;
  updated_at: string | null;
  closed_at: string | null;
  read_at: string;
  category: RegisterCategory;
  excluded: boolean;
  why: Reason[];
}

export interface PendingInclusion {
  project_id: number;
  project: string;
  iid: number;
  decision_id: number;
  reason: string;
  decided_by: string;
  decided_at: string;
}

export interface RegisterPage {
  items: RegisterIssue[];
  total: number;
  pending: PendingInclusion[];
  active: boolean;
  last_read_at: string | null;
}

export interface RegisterFilters {
  category?: RegisterCategory | undefined;
  rule_id?: number | undefined;
  state?: "opened" | "closed" | undefined;
  issue_type?: string | undefined;
  label?: string | undefined;
  assignee?: string | undefined;
  q?: string | undefined;
  period_start?: string | undefined;
  period_end?: string | undefined;
  include_excluded?: boolean | undefined;
}

export const PAGE_SIZE = 50;

export function listRegister(
  filters: RegisterFilters,
  offset: number,
  signal?: AbortSignal,
): Promise<RegisterPage> {
  return getJson(
    "/api/v1/register/issues",
    {
      ...filters,
      include_excluded: filters.include_excluded ? "true" : undefined,
      offset,
      limit: PAGE_SIZE,
    },
    signal,
  );
}

export interface Rule {
  id: number;
  name: string;
  category: RegisterCategory;
  labels: string[];
  project_ids: number[];
  projects: string[];
  issue_types: IssueType[];
  created_by: string;
  created_at: string;
  ended_by: string | null;
  ended_at: string | null;
}

export function listRules(signal?: AbortSignal): Promise<{ rules: Rule[] }> {
  return getJson("/api/v1/register/rules", undefined, signal);
}

export interface NewRule {
  name: string;
  category: RegisterCategory;
  labels: string[];
  project_ids: number[];
  issue_types: IssueType[];
}

export async function createRule(body: NewRule, csrf: string): Promise<Rule> {
  return (await sendJson<Rule>("POST", "/api/v1/register/rules", body, csrf)) as Rule;
}

export async function endRule(id: number, csrf: string): Promise<Rule> {
  return (await sendJson<Rule>(
    "POST",
    `/api/v1/register/rules/${String(id)}/end`,
    undefined,
    csrf,
  )) as Rule;
}

export interface Decision {
  id: number;
  project_id: number;
  iid: number;
  decision: "include" | "exclude";
  category: RegisterCategory | null;
  reason: string;
  decided_by: string;
  decided_at: string;
  ended_by: string | null;
  ended_at: string | null;
}

export interface NewDecision {
  project_id: number;
  iid: number;
  decision: "include" | "exclude";
  category?: RegisterCategory;
  reason: string;
}

export const MIN_REASON = 20;

export async function createDecision(body: NewDecision, csrf: string): Promise<Decision> {
  return (await sendJson<Decision>("POST", "/api/v1/register/decisions", body, csrf)) as Decision;
}

export async function endDecision(id: number, csrf: string): Promise<Decision> {
  return (await sendJson<Decision>(
    "POST",
    `/api/v1/register/decisions/${String(id)}/end`,
    undefined,
    csrf,
  )) as Decision;
}

export async function collectRegister(csrf: string): Promise<{ job_ids: number[] }> {
  return (await sendJson<{ job_ids: number[] }>(
    "POST",
    "/api/v1/register/collect",
    undefined,
    csrf,
  )) as { job_ids: number[] };
}
