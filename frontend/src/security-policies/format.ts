/**
 * Words for discovered GitLab security policies: their types, coverage, why something is unknown,
 * and a plain-language summary of a policy's YAML. Always "GitLab security policy", never just
 * "policy", so they are not confused with Baluarte's baselines.
 */

import type {
  CoverageRow,
  CoverageStatus,
  DiscoveryContext,
  PolicyType,
  TypeAvailability,
} from "../api/securityPolicies";
import { policyTypes } from "../api/securityPolicies";
import { SECURITY_POLICIES_VIEW } from "../policies/format";
import { formatTime } from "../time";
import { readPolicyYaml, type YamlValue } from "./yaml";

export const COVERAGE_VIEW = `${SECURITY_POLICIES_VIEW}/coverage`;

export function securityPolicyHref(id: number): string {
  return `${SECURITY_POLICIES_VIEW}/${String(id)}`;
}

export const typeLabels: Record<PolicyType, string> = {
  scan_execution: "Scan execution",
  approval: "Merge request approval",
  pipeline_execution: "Pipeline execution",
  vulnerability_management: "Vulnerability management",
};

export const shortTypeLabels: Record<PolicyType, string> = {
  scan_execution: "Scan",
  approval: "Approval",
  pipeline_execution: "Pipeline",
  vulnerability_management: "Vulnerability",
};

export const coverageLabels: Record<CoverageStatus, string> = {
  covered: "Covered",
  not_covered: "Not covered",
  unknown: "Unknown",
};

export const availabilityLabels: Record<TypeAvailability, string> = {
  ok: "Readable",
  unsupported: "Not supported by this GitLab",
  unknown: "Unknown",
};

/**
 * Why discovery results can't be trusted to be complete, or null when they can.
 * Unknown is never shown as empty: the caller says coverage is unknown, not empty.
 */
export function discoveryProblem(context: DiscoveryContext): string | null {
  const { collection } = context;
  const unknown = "Coverage is unknown, not empty.";
  if (collection.status === "never" && !collection.running) {
    return `GitLab security policies haven't been collected yet. ${unknown}`;
  }
  let reason: string | null = null;
  if (!context.readable) {
    const unsupported = policyTypes.filter((type) => context.availability[type] === "unsupported");
    reason =
      unsupported.length === policyTypes.length
        ? "this GitLab doesn't offer them to Baluarte's token (they need GitLab Ultimate, and the token needs at least Reporter access to read them)"
        : "the last capability check couldn't confirm that Baluarte's token can read them";
  } else if (collection.status === "failed") {
    const error = collection.last_run?.error?.replace(/\.$/, "");
    reason = `the last collection failed${error ? ` (${error})` : ""}`;
  }
  return reason ? `Baluarte couldn't read GitLab security policies: ${reason}. ${unknown}` : null;
}

/** Types this GitLab can't report on, as a sentence, or null when every type is readable. */
export function typeGaps(context: DiscoveryContext): string | null {
  const gaps = policyTypes.filter((type) => (context.availability[type] ?? "unknown") !== "ok");
  if (gaps.length === 0) return null;
  return gaps
    .map(
      (type) =>
        `${typeLabels[type]}: ${availabilityLabels[context.availability[type] ?? "unknown"]}`,
    )
    .join("; ");
}

export function collectedText(context: DiscoveryContext): string {
  const { collection } = context;
  if (collection.running) return "Collecting now";
  if (!collection.last_success_at) return "Never collected";
  return `Last collected ${formatTime(collection.last_success_at)}`;
}

/** The path of the nearest group above ``path`` that has a row, if any. */
function nearestGroup(path: string, groups: Map<string, CoverageRow>): CoverageRow | undefined {
  const parts = path.split("/");
  for (let end = parts.length - 1; end > 0; end -= 1) {
    const row = groups.get(parts.slice(0, end).join("/"));
    if (row) return row;
  }
  return undefined;
}

/** Why a group or project has the coverage it has, in words. */
export function coverageReason(
  row: CoverageRow,
  kind: "group" | "project",
  groups: Map<string, CoverageRow>,
): string {
  if (row.status === "unknown") {
    return `Unknown: ${row.reason ?? "not every policy type could be read"}. It isn't counted as covered or not covered.`;
  }
  const enabled = policyTypes.flatMap(
    (type) => row.types[type]?.policies.filter((policy) => policy.enabled === true) ?? [],
  );
  if (row.status === "covered") {
    const sources = [...new Set(enabled.map((policy) => policy.via).filter(Boolean))];
    const names = enabled.map((policy) => policy.name);
    const from = sources.length > 0 ? ` from ${sources.join(", ")}` : "";
    return `${String(names.length)} enabled ${names.length === 1 ? "policy reaches" : "policies reach"} it${from}.`;
  }
  const disabled = policyTypes.reduce((sum, type) => sum + (row.types[type]?.disabled ?? 0), 0);
  if (disabled > 0) return "Only disabled GitLab security policies reach it.";
  if (kind === "project") {
    const parent = nearestGroup(row.path, groups);
    if (parent?.status === "covered") {
      return `Its group ${parent.path} is covered, but the policies' scope excludes this project.`;
    }
  }
  if (row.security_policy_project) {
    return `Linked to ${row.security_policy_project}, but none of its policies reach this ${kind}.`;
  }
  return `No policy source project is linked to this ${kind} or the groups above it.`;
}

export interface Notable {
  level: "warn" | "info";
  text: string;
}

export interface PolicySummary {
  /** Plain sentences, in reading order. */
  lines: string[];
  notable: Notable[];
}

type Mapping = Record<string, YamlValue>;

function asMap(value: YamlValue | undefined): Mapping | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function asList(value: YamlValue | undefined): YamlValue[] {
  return Array.isArray(value) ? value : [];
}

function strings(value: YamlValue | undefined): string[] {
  return asList(value).filter((item): item is string => typeof item === "string");
}

function words(value: string): string {
  return value.replace(/_/g, " ");
}

const scanLabels: Record<string, string> = {
  secret_detection: "secret detection",
  sast: "SAST",
  sast_iac: "SAST IaC",
  dependency_scanning: "dependency scanning",
  container_scanning: "container scanning",
  dast: "DAST",
  cluster_image_scanning: "cluster image scanning",
  api_fuzzing: "API fuzzing",
  coverage_fuzzing: "coverage fuzzing",
};

function listText(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1) ?? ""}`;
}

const branchTypes: Record<string, string> = {
  default: "the default branch",
  protected: "protected branches",
  all: "all branches",
};

function branchText(rule: Mapping): string {
  const branchType = rule.branch_type;
  if (typeof branchType === "string") {
    const target = branchType.replace(/^target_/, "");
    return branchTypes[target] ?? `${words(target)} branches`;
  }
  const branches = strings(rule.branches);
  return branches.length > 0 ? `branches ${branches.join(", ")}` : "every branch";
}

function scanRuleText(rule: Mapping): string {
  if (rule.type === "schedule") {
    const cadence = typeof rule.cadence === "string" ? ` (${rule.cadence})` : "";
    return `on a schedule${cadence} for ${branchText(rule)}`;
  }
  if (typeof rule.branch_type === "string" && rule.branch_type.startsWith("target_")) {
    return `in merge request pipelines targeting ${branchText(rule)}`;
  }
  return `in pipelines on ${branchText(rule)}`;
}

function skipCi(policy: Mapping, notable: Notable[]): void {
  if (asMap(policy.skip_ci)?.allowed === true) {
    notable.push({
      level: "warn",
      text: "skip_ci is allowed: anyone can skip this policy's jobs with [skip ci].",
    });
  }
}

function scanSummary(policy: Mapping, lines: string[], notable: Notable[]): void {
  const scans = asList(policy.actions)
    .map((action) => asMap(action)?.scan)
    .filter((scan): scan is string => typeof scan === "string");
  if (scans.length > 0)
    lines.push(`Runs ${listText(scans.map((scan) => scanLabels[scan] ?? words(scan)))}.`);
  const rules = asList(policy.rules)
    .map(asMap)
    .filter((rule): rule is Mapping => rule !== null);
  if (rules.length > 0) lines.push(`When: ${rules.map(scanRuleText).join("; ")}.`);
  skipCi(policy, notable);
}

const approvalSettingLabels: Record<string, string> = {
  prevent_approval_by_author: "authors can't approve their own merge requests",
  prevent_approval_by_commit_author: "commit authors can't approve",
  remove_approvals_with_new_commit: "new commits remove approvals",
  require_password_to_approve: "approving needs the user's password",
  block_branch_modification: "branch protection can't be changed",
  prevent_pushing_and_force_pushing: "pushing and force-pushing are blocked",
};

function approvalSummary(policy: Mapping, lines: string[], notable: Notable[]): void {
  for (const action of asList(policy.actions).map(asMap)) {
    if (action?.type !== "require_approval") continue;
    const count = typeof action.approvals_required === "number" ? action.approvals_required : null;
    const who = [
      ...strings(action.role_approvers).map((role) => `${role}s`),
      ...strings(action.user_approvers),
      ...strings(action.group_approvers),
    ];
    const required =
      count === null
        ? "Requires approval"
        : `Requires ${String(count)} ${count === 1 ? "approval" : "approvals"}`;
    lines.push(`${required}${who.length > 0 ? ` from ${listText(who)}` : ""}.`);
  }
  const rules = asList(policy.rules)
    .map(asMap)
    .filter((rule): rule is Mapping => rule !== null);
  for (const rule of rules) {
    const type = typeof rule.type === "string" ? rule.type : "";
    if (type === "any_merge_request") lines.push(`For any merge request into ${branchText(rule)}.`);
    else if (type === "license_finding") {
      const licenses = strings(rule.license_types);
      lines.push(
        `When a merge request ${rule.match_on_inclusion_license === false ? "adds a license not on its list" : "adds one of these licenses"}${
          licenses.length > 0 ? ` (${String(licenses.length)} listed)` : ""
        }.`,
      );
    } else if (type === "scan_finding")
      lines.push(`When scanners report findings on ${branchText(rule)}.`);
    else if (type) lines.push(`Rule: ${words(type)} on ${branchText(rule)}.`);
  }
  const settings = asMap(policy.approval_settings);
  if (settings) {
    const on = Object.entries(settings)
      .filter(([, value]) => value === true)
      .map(([key]) => approvalSettingLabels[key] ?? words(key));
    if (on.length > 0) lines.push(`Settings: ${on.join("; ")}.`);
    if (settings.prevent_approval_by_author === false) {
      notable.push({ level: "warn", text: "Authors may approve their own merge requests." });
    }
  }
  if (asMap(policy.fallback_behavior)?.fail === "open") {
    notable.push({
      level: "warn",
      text: "Fails open: if GitLab can't evaluate the policy, merge requests are not blocked.",
    });
  }
}

function pipelineSummary(policy: Mapping, lines: string[], notable: Notable[]): void {
  const includes = asList(asMap(policy.content)?.include)
    .map(asMap)
    .filter((entry): entry is Mapping => entry !== null);
  for (const entry of includes) {
    const project = typeof entry.project === "string" ? entry.project : null;
    const file = typeof entry.file === "string" ? entry.file : null;
    if (project || file)
      lines.push(`Includes ${file ?? "a CI file"}${project ? ` from ${project}` : ""}.`);
  }
  if (typeof policy.pipeline_config_strategy === "string") {
    lines.push(`Strategy: ${words(policy.pipeline_config_strategy)}.`);
  }
  skipCi(policy, notable);
}

function vulnerabilitySummary(policy: Mapping, lines: string[]): void {
  const actions = asList(policy.actions)
    .map((action) => asMap(action)?.type)
    .filter((type): type is string => typeof type === "string");
  if (actions.length > 0) lines.push(`Actions: ${listText(actions.map(words))}.`);
}

/** A summary of one policy's YAML, or null when the YAML can't be read (the YAML still shows). */
export function summarizePolicy(type: PolicyType, yaml: string | null): PolicySummary | null {
  const policy = asMap(readPolicyYaml(yaml));
  if (!policy) return null;
  const lines: string[] = [];
  const notable: Notable[] = [];
  if (type === "scan_execution") scanSummary(policy, lines, notable);
  else if (type === "approval") approvalSummary(policy, lines, notable);
  else if (type === "pipeline_execution") pipelineSummary(policy, lines, notable);
  else vulnerabilitySummary(policy, lines);
  return { lines, notable };
}
