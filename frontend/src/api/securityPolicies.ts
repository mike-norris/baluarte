/** Discovered GitLab security policies (docs/GITLAB_INTEGRATION.md#security-policies). */

import { getJson } from "./client";
import type { KindFreshness } from "./gitlab";
import { sendJson } from "./send";

const BASE = "/api/v1/gitlab/security-policies";
export const POLICIES_PAGE = 100;

export const policyTypes = [
  "scan_execution",
  "approval",
  "pipeline_execution",
  "vulnerability_management",
] as const;
export type PolicyType = (typeof policyTypes)[number];

export type TypeAvailability = "ok" | "unsupported" | "unknown";
export type CoverageStatus = "covered" | "not_covered" | "unknown";

/** Whether discovery could read policies, per type, and how recent its collection is. */
export interface DiscoveryContext {
  readable: boolean;
  availability: Partial<Record<PolicyType, TypeAvailability>>;
  collection: KindFreshness;
}

export interface LinkedFrom {
  type: "group" | "project";
  path: string;
}

export interface SecurityPolicySummary {
  id: number;
  policy_type: PolicyType;
  name: string;
  description: string | null;
  enabled: boolean | null;
  /** ``linked_source`` when Baluarte can't see the policy project and keys it by the linked path. */
  origin_kind: "policy_project" | "linked_source";
  origin_path: string;
  linked_from: LinkedFrom[];
  state: "active" | "missing";
  yaml_digest: string | null;
  gitlab_updated_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  reaches: { groups: number; projects: number };
}

export interface SecurityPolicyPage extends DiscoveryContext {
  items: SecurityPolicySummary[];
  next_after_id: number | null;
}

export interface PolicyScope {
  compliance_frameworks: string[];
  including_projects: string[];
  excluding_projects: string[];
  including_groups: string[];
  excluding_groups: string[];
  excluding_archived_projects: boolean | null;
  excluding_personal_projects: boolean | null;
  truncated: boolean;
}

export interface ReachedTarget {
  id: number;
  gitlab_id: number;
  path: string;
  via: string | null;
  inherited: boolean | null;
  observation_id: number;
  collected_at: string;
}

export interface SecurityPolicyDetail extends SecurityPolicySummary, DiscoveryContext {
  yaml: string | null;
  scope: PolicyScope;
  reached: { groups: ReachedTarget[]; projects: ReachedTarget[] };
}

export interface CoverageCounts {
  covered: number;
  not_covered: number;
  unknown: number;
}

export interface TypeCoverage {
  status: TypeAvailability;
  truncated: boolean;
  enabled: number;
  disabled: number;
  policies: {
    id: number | null;
    name: string;
    enabled: boolean | null;
    via: string | null;
    inherited: boolean | null;
  }[];
}

export interface CoverageRow {
  id: number;
  gitlab_id: number;
  path: string;
  status: CoverageStatus;
  /** Why the status is unknown. */
  reason: string | null;
  /** The policy source project linked directly to this group or project. */
  security_policy_project: string | null;
  observation: {
    id: number;
    collected_at: string;
    collection_status: string;
    error: string | null;
  } | null;
  types: Partial<Record<PolicyType, TypeCoverage>>;
}

export interface Coverage extends DiscoveryContext {
  summary: { groups: CoverageCounts; projects: CoverageCounts };
  groups: CoverageRow[];
  projects: CoverageRow[];
}

export interface PolicyFilters {
  policyType: PolicyType | "";
  includeMissing: boolean;
}

export function listSecurityPolicies(
  filters: PolicyFilters,
  after: number | null,
  signal?: AbortSignal,
): Promise<SecurityPolicyPage> {
  return getJson<SecurityPolicyPage>(
    BASE,
    {
      policy_type: filters.policyType,
      state: filters.includeMissing ? null : "active",
      after_id: after,
      limit: POLICIES_PAGE,
    },
    signal,
  );
}

export function getSecurityPolicy(id: number, signal?: AbortSignal): Promise<SecurityPolicyDetail> {
  return getJson<SecurityPolicyDetail>(`${BASE}/${String(id)}`, undefined, signal);
}

export function getCoverage(signal?: AbortSignal, kind?: "group" | "project"): Promise<Coverage> {
  return getJson<Coverage>(`${BASE}/coverage`, kind ? { kind } : undefined, signal);
}

/** Queue a discovery run now (``settings:manage``); GitLab is read by the worker, not here. */
export async function discoverSecurityPolicies(csrfToken: string): Promise<number> {
  const queued = await sendJson<{ job_id: number }>("POST", `${BASE}/discover`, {}, csrfToken);
  return queued?.job_id ?? 0;
}
