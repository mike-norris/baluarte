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

export interface IssueDetail {
  project_id: number;
  project: string;
  iid: number;
  issue: RegisterIssue | null;
  auditable: boolean;
  decisions: Decision[];
}

export function getIssue(
  projectId: number,
  iid: number,
  signal?: AbortSignal,
): Promise<IssueDetail> {
  return getJson(`/api/v1/register/issues/${String(projectId)}/${String(iid)}`, undefined, signal);
}

export interface CapturedNote {
  id: number | null;
  author: string | null;
  body: string | null;
  system: boolean;
  internal: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface HistoryEvent {
  kind: "state" | "label" | "milestone";
  id: number | null;
  user: string | null;
  created_at: string | null;
  action: string | null;
  state: string | null;
  label: string | null;
  milestone: string | null;
  merge_request: string | null;
}

export interface CapturedMergeRequest {
  project_id: number | null;
  iid: number | null;
  relation: "closes" | "related";
  title: string | null;
  state: string | null;
  web_url: string | null;
  author: string | null;
  reviewers: string[];
  target_branch: string | null;
  merged_at: string | null;
  merged_by: string | null;
  merge_commit_sha: string | null;
  approvals: { approved: boolean; approved_by: string[]; approvals_required: number | null } | null;
  pipelines:
    | { id: number | null; status: string | null; ref: string | null; web_url: string | null }[]
    | null;
}

export interface CapturedAttachment {
  secret: string;
  filename: string;
  referenced_in: string[];
  status: string;
  sha256: string | null;
  size_bytes: number | null;
  content_type: string | null;
  error: string | null;
}

export interface Capture {
  issue?: {
    title: string | null;
    description: string | null;
    state: string | null;
    closed_by: string | null;
    web_url: string | null;
  };
  notes?: CapturedNote[];
  history?: HistoryEvent[];
  links?: {
    project_id: number | null;
    iid: number | null;
    title: string | null;
    state: string | null;
    web_url: string | null;
    link_type: string | null;
  }[];
  merge_requests?: CapturedMergeRequest[];
  releases?: {
    tag_name: string | null;
    name: string | null;
    released_at: string | null;
    web_url: string | null;
  }[];
  attachments?: CapturedAttachment[];
  sources: Record<string, string>;
}

export interface IssueEvidence {
  project_id: number;
  iid: number;
  captured: boolean;
  status: "complete" | "partial" | "failed" | null;
  gaps: string[];
  captured_at: string | null;
  captured_for: string | null;
  observation_id: number | null;
  payload_digest: string | null;
  digest_verified: boolean | null;
  capture: Capture | null;
}

export function getIssueEvidence(
  projectId: number,
  iid: number,
  signal?: AbortSignal,
): Promise<IssueEvidence> {
  return getJson(
    `/api/v1/register/issues/${String(projectId)}/${String(iid)}/evidence`,
    undefined,
    signal,
  );
}

export function fileHref(sha256: string): string {
  return `/api/v1/register/files/${sha256}`;
}

export function issueHref(projectId: number, iid: number): string {
  return `/register/issues/${String(projectId)}/${String(iid)}`;
}
