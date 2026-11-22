/**
 * Rule coverage (GET /api/v1/policies/coverage): which rule types the baselines in force use,
 * and which controls that leaves without automated evidence (docs/POLICY_ENGINE.md).
 */

import { getJson } from "../api/client";
import {
  APPROVAL_TYPE,
  BRANCH_TYPE,
  DEPLOYED_TYPE,
  MEMBERS_TYPE,
  PIPELINE_TYPE,
  PROJECT_APPROVALS_TYPE,
  REVIEWED_TYPE,
  SCAN_TYPE,
  TOKENS_TYPE,
  TWO_FACTOR_TYPE,
  VISIBILITY_TYPE,
} from "./yaml";

export const LABELS_TYPE = "gitlab.labels.consistent";
export const SECURITY_FEATURES_TYPE = "github.repository.security_features";

export interface RuleTypeCoverage {
  rule_type: string;
  /** A baseline version applied somewhere has a rule of this type. */
  in_force: boolean;
  baselines: { name: string; version: number }[];
  /** From detected capabilities; null when they can't tell. */
  supported: boolean | null;
  capability: string | null;
}

export interface UncoveredControl {
  framework: string;
  control_id: string;
  title: string;
  area: string;
  rule_types: string[];
  status_change: "not_applicable" | "manual" | null;
}

export interface BaselineRules {
  name: string;
  latest_version: number;
  applied: boolean;
  rule_types: string[];
}

export interface Coverage {
  rule_types: RuleTypeCoverage[];
  controls: UncoveredControl[];
  baselines: BaselineRules[];
  label_taxonomy_version: number | null;
  capabilities_checked_at: string | null;
}

export function getCoverage(signal?: AbortSignal): Promise<Coverage> {
  return getJson("/api/v1/policies/coverage", undefined, signal);
}

/** Each rule type's name, and one line on what it proves when it passes. */
export const ruleTypeInfo: Record<string, { name: string; proves: string }> = {
  [VISIBILITY_TYPE]: {
    name: "Project visibility",
    proves: "Code is not readable beyond the visibility you allow.",
  },
  [BRANCH_TYPE]: {
    name: "Protected default branch",
    proves: "Changes reach the default branch only through its protection rules.",
  },
  [PROJECT_APPROVALS_TYPE]: {
    name: "Project merge request approvals",
    proves: "Each project's own settings require approval before a merge.",
  },
  [MEMBERS_TYPE]: {
    name: "Privileged members",
    proves: "Maintainer and Owner access is limited, and no inactive account keeps it.",
  },
  [TWO_FACTOR_TYPE]: {
    name: "Group two-factor authentication",
    proves: "People sign in with a second factor.",
  },
  [TOKENS_TYPE]: {
    name: "Project access tokens",
    proves: "Project tokens expire instead of living forever.",
  },
  [REVIEWED_TYPE]: {
    name: "Changes reviewed",
    proves: "Every change merged was approved by someone other than its author.",
  },
  [PIPELINE_TYPE]: {
    name: "Pipelines passed",
    proves: "Every change merged passed its pipeline.",
  },
  [DEPLOYED_TYPE]: {
    name: "Deployed after review",
    proves: "What reached production was a reviewed change.",
  },
  [SCAN_TYPE]: {
    name: "Scan execution policy",
    proves: "GitLab runs the required security scans on every default branch.",
  },
  [APPROVAL_TYPE]: {
    name: "Merge request approval policy",
    proves: "A GitLab approval policy requires approval of every merge request.",
  },
  [LABELS_TYPE]: {
    name: "Label consistency",
    proves: "Labels follow the label taxonomy.",
  },
  [SECURITY_FEATURES_TYPE]: {
    name: "GitHub security features",
    proves: "Secret scanning, Dependabot and code scanning are on for each GitHub repository.",
  },
};

export function ruleTypeName(ruleType: string): string {
  return ruleTypeInfo[ruleType]?.name ?? ruleType;
}

/** The rule types that need GitLab security policies (GitLab Ultimate). */
export const SECURITY_POLICY_TYPES: readonly string[] = [SCAN_TYPE, APPROVAL_TYPE];

/** The rule types the recommended baseline switches on, on every GitLab tier. */
export const EVERY_TIER_TYPES: readonly string[] = [
  VISIBILITY_TYPE,
  BRANCH_TYPE,
  PROJECT_APPROVALS_TYPE,
  MEMBERS_TYPE,
  TWO_FACTOR_TYPE,
  TOKENS_TYPE,
  REVIEWED_TYPE,
  PIPELINE_TYPE,
  DEPLOYED_TYPE,
];

/** Whether the instance's detected capabilities say security policies can be read. */
export function securityPoliciesReadable(coverage: Coverage | undefined): boolean {
  if (!coverage) return false;
  return SECURITY_POLICY_TYPES.every(
    (type) => coverage.rule_types.find((item) => item.rule_type === type)?.supported === true,
  );
}

/** The rule types the recommended baseline uses on this instance. */
export function recommendedTypes(securityPolicies: boolean): string[] {
  return securityPolicies ? [...EVERY_TIER_TYPES, ...SECURITY_POLICY_TYPES] : [...EVERY_TIER_TYPES];
}

/** Recommended rule types a baseline's latest version doesn't have, in recommended order. */
export function missingRecommended(baseline: BaselineRules, securityPolicies: boolean): string[] {
  return recommendedTypes(securityPolicies).filter((type) => !baseline.rule_types.includes(type));
}

/** Told in the editor when the security policy rules stay off. */
export const SECURITY_POLICIES_OFF =
  "The two GitLab security policy rules are off: they need GitLab Ultimate, and this instance does not report security policies.";
