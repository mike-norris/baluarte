/** Baseline forms and coverage for tests. */

import { LABELS_TYPE, type Coverage } from "./coverage";
import {
  APPROVAL_TYPE,
  BRANCH_TYPE,
  DEPLOYED_TYPE,
  emptyBaseline,
  MEMBERS_TYPE,
  PIPELINE_TYPE,
  PROJECT_APPROVALS_TYPE,
  REVIEWED_TYPE,
  SCAN_TYPE,
  TOKENS_TYPE,
  TWO_FACTOR_TYPE,
  VISIBILITY_TYPE,
  type BaselineForm,
} from "./yaml";

/** A baseline with only project visibility and default-branch protection on. */
export function basicBaseline(name = "recommended-baseline"): BaselineForm {
  const form = emptyBaseline();
  return {
    ...form,
    name,
    description: "No public projects; every default branch protected.",
    visibility: { ...form.visibility, enabled: true },
    branch: { ...form.branch, enabled: true },
  };
}

const ALL_TYPES = [
  VISIBILITY_TYPE,
  BRANCH_TYPE,
  SCAN_TYPE,
  APPROVAL_TYPE,
  PROJECT_APPROVALS_TYPE,
  MEMBERS_TYPE,
  TWO_FACTOR_TYPE,
  TOKENS_TYPE,
  REVIEWED_TYPE,
  PIPELINE_TYPE,
  DEPLOYED_TYPE,
  LABELS_TYPE,
];

/**
 * Coverage where ``starter-baseline`` v2 (visibility and default-branch protection) is applied,
 * and security policies are readable only when ``securityPolicies`` is set.
 */
export function coverageFixture({
  securityPolicies = false,
  inForce = [VISIBILITY_TYPE, BRANCH_TYPE],
}: { securityPolicies?: boolean; inForce?: string[] } = {}): Coverage {
  const security = [SCAN_TYPE, APPROVAL_TYPE];
  return {
    rule_types: ALL_TYPES.map((type) => ({
      rule_type: type,
      in_force: inForce.includes(type),
      baselines: inForce.includes(type) ? [{ name: "starter-baseline", version: 2 }] : [],
      supported: security.includes(type) ? securityPolicies || null : true,
      capability: security.includes(type) ? "security_policies.read" : null,
    })),
    controls: [
      {
        framework: "nist_800_53_moderate",
        control_id: "RA-5",
        title: "Systems are scanned for vulnerabilities",
        area: "Vulnerability management",
        rule_types: [SCAN_TYPE],
        status_change: null,
      },
      {
        framework: "nist_800_53_moderate",
        control_id: "IA-2(1)",
        title: "Privileged accounts sign in with more than one factor",
        area: "Authentication",
        rule_types: [TWO_FACTOR_TYPE],
        status_change: "manual",
      },
    ],
    baselines: [
      {
        name: "starter-baseline",
        latest_version: 2,
        applied: true,
        rule_types: inForce,
      },
    ],
    label_taxonomy_version: null,
    capabilities_checked_at: "2026-11-01T10:00:00Z",
  };
}
