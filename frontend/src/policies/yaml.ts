/**
 * The baseline form and its YAML.
 *
 * The form covers the rule types the API knows, one rule of each. The two GitLab security policy
 * rules are off unless added, since they need GitLab Ultimate. A saved version with
 * anything else (two rules of one type, or a type this UI predates) opens as YAML instead, so
 * the form never drops part of a baseline. The API validates every save; these checks only
 * catch mistakes before the round trip.
 */

import { NAME_PATTERN } from "../api/policies";

export const severities = ["low", "medium", "high", "critical"] as const;
export type Severity = (typeof severities)[number];

export const visibilities = ["private", "internal", "public"] as const;
export type Visibility = (typeof visibilities)[number];

export const severityLabels: Record<Severity, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  critical: "Critical",
};

export const visibilityLabels: Record<Visibility, string> = {
  private: "Private",
  internal: "Internal",
  public: "Public",
};

export interface VisibilityRule {
  enabled: boolean;
  id: string;
  allowed: Visibility[];
  severity: Severity;
  description: string;
}

export interface BranchRule {
  enabled: boolean;
  id: string;
  severity: Severity;
  description: string;
}

export const scans = [
  "secret_detection",
  "sast",
  "sast_iac",
  "dependency_scanning",
  "container_scanning",
  "dast",
] as const;
export type Scan = (typeof scans)[number];

export const scanLabels: Record<Scan, string> = {
  secret_detection: "Secret detection",
  sast: "SAST",
  sast_iac: "Infrastructure as code scanning",
  dependency_scanning: "Dependency scanning",
  container_scanning: "Container scanning",
  dast: "DAST",
};

/** A GitLab scan execution policy must run these scans on the default branch. */
export interface ScanRule {
  enabled: boolean;
  id: string;
  scans: Scan[];
  /** When false, a policy that lets a pipeline skip CI doesn't count. */
  allowSkipCi: boolean;
  severity: Severity;
  description: string;
}

/** A GitLab merge request approval policy must require this many approvals. */
export interface ApprovalRule {
  enabled: boolean;
  id: string;
  minApprovals: number;
  forbidAuthorApproval: boolean;
  forbidCommitterApproval: boolean;
  severity: Severity;
  description: string;
}

export const MIN_APPROVALS = 1;
export const MAX_APPROVALS = 20;

export interface BaselineForm {
  name: string;
  description: string;
  /** "all" is the scope ["*"]: everywhere the baseline is applied. */
  scope: "all" | "groups";
  groups: string[];
  visibility: VisibilityRule;
  branch: BranchRule;
  /** Off unless added: these depend on GitLab security policies, which need GitLab Ultimate. */
  scan: ScanRule;
  approval: ApprovalRule;
}

export const VISIBILITY_TYPE = "gitlab.project.visibility";
export const BRANCH_TYPE = "gitlab.branch.default_protected";
export const SCAN_TYPE = "gitlab.security_policy.scan_execution";
export const APPROVAL_TYPE = "gitlab.security_policy.approval";

/** The scan rule as "Add rule" first shows it. */
export function offScanRule(): ScanRule {
  return {
    enabled: false,
    id: "required-scans",
    scans: ["secret_detection", "sast"],
    allowSkipCi: true,
    severity: "high",
    description: "",
  };
}

/** The approval rule as "Add rule" first shows it. */
export function offApprovalRule(): ApprovalRule {
  return {
    enabled: false,
    id: "required-approvals",
    minApprovals: 1,
    forbidAuthorApproval: false,
    forbidCommitterApproval: false,
    severity: "medium",
    description: "",
  };
}

/** The recommended baseline: no public projects, and every default branch protected. */
export function recommendedBaseline(): BaselineForm {
  return {
    name: "recommended-baseline",
    description: "No public projects; every default branch protected.",
    scope: "all",
    groups: [],
    visibility: {
      enabled: true,
      id: "project-visibility",
      allowed: ["private", "internal"],
      severity: "high",
      description: "Projects must not be public.",
    },
    branch: {
      enabled: true,
      id: "protected-default-branch",
      severity: "medium",
      description: "The default branch must be covered by a protected-branch rule.",
    },
    scan: offScanRule(),
    approval: offApprovalRule(),
  };
}

/** A blank baseline: the project rules offered, none switched on. */
export function emptyBaseline(): BaselineForm {
  const recommended = recommendedBaseline();
  return {
    ...recommended,
    name: "",
    description: "",
    visibility: { ...recommended.visibility, enabled: false },
    branch: { ...recommended.branch, enabled: false },
  };
}

/** A double-quoted YAML scalar; JSON string syntax is valid YAML and escapes everything. */
function quoted(text: string): string {
  return JSON.stringify(text);
}

export function toYaml(form: BaselineForm): string {
  const lines = [
    "apiVersion: baluarte.io/v1alpha1",
    "kind: GitLabBaseline",
    "metadata:",
    `  name: ${form.name.trim()}`,
  ];
  if (form.description.trim()) lines.push(`  description: ${quoted(form.description.trim())}`);
  const groups = form.scope === "all" ? ["*"] : cleanGroups(form.groups);
  lines.push("spec:", "  scope:", `    groups: [${groups.map(quoted).join(", ")}]`, "  rules:");
  if (form.visibility.enabled) {
    const rule = form.visibility;
    const allowed = visibilities.filter((value) => rule.allowed.includes(value));
    lines.push(
      `    - id: ${rule.id}`,
      `      type: ${VISIBILITY_TYPE}`,
      `      allowed: [${allowed.join(", ")}]`,
      `      severity: ${rule.severity}`,
    );
    if (rule.description.trim())
      lines.push(`      description: ${quoted(rule.description.trim())}`);
  }
  if (form.branch.enabled) {
    const rule = form.branch;
    lines.push(
      `    - id: ${rule.id}`,
      `      type: ${BRANCH_TYPE}`,
      "      required: true",
      `      severity: ${rule.severity}`,
    );
    if (rule.description.trim())
      lines.push(`      description: ${quoted(rule.description.trim())}`);
  }
  if (form.scan.enabled) {
    const rule = form.scan;
    lines.push(
      `    - id: ${rule.id}`,
      `      type: ${SCAN_TYPE}`,
      `      scans: [${orderedScans(rule.scans).join(", ")}]`,
      `      allow_skip_ci: ${String(rule.allowSkipCi)}`,
      `      severity: ${rule.severity}`,
    );
    if (rule.description.trim())
      lines.push(`      description: ${quoted(rule.description.trim())}`);
  }
  if (form.approval.enabled) {
    const rule = form.approval;
    lines.push(
      `    - id: ${rule.id}`,
      `      type: ${APPROVAL_TYPE}`,
      `      min_approvals: ${String(rule.minApprovals)}`,
      `      forbid_author_approval: ${String(rule.forbidAuthorApproval)}`,
      `      forbid_committer_approval: ${String(rule.forbidCommitterApproval)}`,
      `      severity: ${rule.severity}`,
    );
    if (rule.description.trim())
      lines.push(`      description: ${quoted(rule.description.trim())}`);
  }
  return `${lines.join("\n")}\n`;
}

/** The form in the shape of a saved version's parsed document, for showing it before saving. */
export function toDocument(form: BaselineForm): Record<string, unknown> {
  const rules: Record<string, unknown>[] = [];
  if (form.visibility.enabled) {
    rules.push({
      id: form.visibility.id,
      type: VISIBILITY_TYPE,
      allowed: form.visibility.allowed,
      severity: form.visibility.severity,
      description: form.visibility.description || null,
    });
  }
  if (form.branch.enabled) {
    rules.push({
      id: form.branch.id,
      type: BRANCH_TYPE,
      required: true,
      severity: form.branch.severity,
      description: form.branch.description || null,
    });
  }
  if (form.scan.enabled) {
    rules.push({
      id: form.scan.id,
      type: SCAN_TYPE,
      scans: orderedScans(form.scan.scans),
      allow_skip_ci: form.scan.allowSkipCi,
      severity: form.scan.severity,
      description: form.scan.description || null,
    });
  }
  if (form.approval.enabled) {
    rules.push({
      id: form.approval.id,
      type: APPROVAL_TYPE,
      min_approvals: form.approval.minApprovals,
      forbid_author_approval: form.approval.forbidAuthorApproval,
      forbid_committer_approval: form.approval.forbidCommitterApproval,
      severity: form.approval.severity,
      description: form.approval.description || null,
    });
  }
  return {
    apiVersion: "baluarte.io/v1alpha1",
    kind: "GitLabBaseline",
    metadata: { name: form.name.trim(), description: form.description.trim() || null },
    spec: {
      scope: { groups: form.scope === "all" ? ["*"] : cleanGroups(form.groups) },
      rules,
    },
  };
}

function orderedScans(chosen: readonly unknown[]): Scan[] {
  return scans.filter((scan) => chosen.includes(scan));
}

function cleanGroups(groups: string[]): string[] {
  const seen = new Set<string>();
  return groups
    .map((group) => group.trim().replace(/^\/+|\/+$/g, ""))
    .filter((group) => {
      if (!group || seen.has(group)) return false;
      seen.add(group);
      return true;
    });
}

/** Problems to fix before saving, in the order the form shows its fields. */
export function formProblems(form: BaselineForm): string[] {
  const problems: string[] = [];
  if (!NAME_PATTERN.test(form.name.trim())) {
    problems.push(
      "Name: use 1 to 63 lowercase letters, digits and hyphens, starting and ending with a letter or digit.",
    );
  }
  if (form.scope === "groups" && cleanGroups(form.groups).length === 0) {
    problems.push("Groups: enter at least one group path, or choose every group.");
  }
  const rules = [form.visibility, form.branch, form.scan, form.approval];
  if (!rules.some((rule) => rule.enabled)) {
    problems.push("Rules: turn on at least one rule.");
  }
  if (form.visibility.enabled && form.visibility.allowed.length === 0) {
    problems.push("Project visibility: allow at least one visibility.");
  }
  if (form.scan.enabled && form.scan.scans.length === 0) {
    problems.push("Required scans: choose at least one scan.");
  }
  const approvals = form.approval.minApprovals;
  if (
    form.approval.enabled &&
    (!Number.isInteger(approvals) || approvals < MIN_APPROVALS || approvals > MAX_APPROVALS)
  ) {
    problems.push(
      `Required approvals: enter a whole number from ${String(MIN_APPROVALS)} to ${String(MAX_APPROVALS)}.`,
    );
  }
  return problems;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSeverity(value: unknown): value is Severity {
  return severities.some((severity) => severity === value);
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isScan(value: unknown): value is Scan {
  return scans.some((scan) => scan === value);
}

/** An optional boolean field: its value, ``fallback`` when absent, or null when not a boolean. */
function flag(value: unknown, fallback: boolean): boolean | null {
  if (value === undefined || value === null) return fallback;
  return typeof value === "boolean" ? value : null;
}

/**
 * The form for a saved version's parsed ``document``, or null when the form cannot show all of
 * it. Editing then happens in YAML.
 */
export function fromDocument(document: unknown): BaselineForm | null {
  if (!isRecord(document) || !isRecord(document.metadata) || !isRecord(document.spec)) return null;
  const { metadata, spec } = document;
  if (!isRecord(spec.scope) || !Array.isArray(spec.scope.groups) || !Array.isArray(spec.rules)) {
    return null;
  }
  const groups = spec.scope.groups.filter((group): group is string => typeof group === "string");
  const form = emptyBaseline();
  form.name = text(metadata.name);
  form.description = text(metadata.description);
  if (groups.includes("*")) {
    if (groups.length !== 1) return null;
    form.scope = "all";
  } else {
    form.scope = "groups";
    form.groups = groups;
  }
  for (const rule of spec.rules) {
    if (!isRecord(rule) || typeof rule.id !== "string") return null;
    const severity = isSeverity(rule.severity) ? rule.severity : "medium";
    if (rule.type === VISIBILITY_TYPE && !form.visibility.enabled && Array.isArray(rule.allowed)) {
      form.visibility = {
        enabled: true,
        id: rule.id,
        allowed: visibilities.filter((value) => (rule.allowed as unknown[]).includes(value)),
        severity,
        description: text(rule.description),
      };
    } else if (rule.type === BRANCH_TYPE && !form.branch.enabled && rule.required === true) {
      form.branch = { enabled: true, id: rule.id, severity, description: text(rule.description) };
    } else if (rule.type === SCAN_TYPE && !form.scan.enabled && Array.isArray(rule.scans)) {
      const allowSkipCi = flag(rule.allow_skip_ci, true);
      if (!rule.scans.every(isScan) || allowSkipCi === null) return null;
      form.scan = {
        enabled: true,
        id: rule.id,
        scans: orderedScans(rule.scans),
        allowSkipCi,
        severity,
        description: text(rule.description),
      };
    } else if (
      rule.type === APPROVAL_TYPE &&
      !form.approval.enabled &&
      typeof rule.min_approvals === "number" &&
      Number.isInteger(rule.min_approvals)
    ) {
      const author = flag(rule.forbid_author_approval, false);
      const committer = flag(rule.forbid_committer_approval, false);
      if (author === null || committer === null) return null;
      form.approval = {
        enabled: true,
        id: rule.id,
        minApprovals: rule.min_approvals,
        forbidAuthorApproval: author,
        forbidCommitterApproval: committer,
        severity,
        description: text(rule.description),
      };
    } else {
      return null;
    }
  }
  return form;
}

export interface ReadableRule {
  id: string;
  sentence: string;
  detail: string | null;
  severity: Severity;
}

function joinWords(words: string[], conjunction = "or"): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} ${conjunction} ${words[words.length - 1] ?? ""}`;
}

/** "secret detection and SAST": acronyms keep their capitals. */
export function scanWords(chosen: Scan[]): string {
  return joinWords(
    chosen.map((scan) => {
      const label = scanLabels[scan];
      return label === label.toUpperCase() ? label : label.toLowerCase();
    }),
    "and",
  );
}

/** Each rule of a saved version as a sentence, for people who don't read YAML. */
export function readableRules(document: unknown): ReadableRule[] {
  if (!isRecord(document) || !isRecord(document.spec) || !Array.isArray(document.spec.rules)) {
    return [];
  }
  return document.spec.rules.filter(isRecord).map((rule) => {
    const id = text(rule.id);
    const severity = isSeverity(rule.severity) ? rule.severity : "medium";
    const description = text(rule.description) || null;
    if (rule.type === VISIBILITY_TYPE && Array.isArray(rule.allowed)) {
      const allowed = visibilities.filter((value) => (rule.allowed as unknown[]).includes(value));
      const sentence =
        allowed.length === 1 && allowed[0] === "private"
          ? "Projects are private"
          : !allowed.includes("public")
            ? "Projects are not public"
            : `Projects are ${joinWords(allowed.map((value) => visibilityLabels[value].toLowerCase()))}`;
      return {
        id,
        sentence,
        detail: `Allowed: ${allowed.map((value) => visibilityLabels[value].toLowerCase()).join(", ")}.`,
        severity,
      };
    }
    if (rule.type === BRANCH_TYPE) {
      return {
        id,
        sentence:
          rule.required === true
            ? "Every default branch is protected"
            : "Default branch protection is not required",
        detail: description,
        severity,
      };
    }
    if (rule.type === SCAN_TYPE && Array.isArray(rule.scans)) {
      const skip =
        rule.allow_skip_ci === false
          ? "Only policies that forbid skipping CI count; one without a skip_ci setting can be skipped."
          : null;
      return {
        id,
        sentence: `Projects run ${scanWords(orderedScans(rule.scans))} on the default branch through a GitLab scan execution policy`,
        detail: [skip, description].filter(Boolean).join(" ") || null,
        severity,
      };
    }
    if (rule.type === APPROVAL_TYPE && typeof rule.min_approvals === "number") {
      const count = rule.min_approvals;
      const forbidden = [
        rule.forbid_author_approval === true ? "the author" : null,
        rule.forbid_committer_approval === true ? "anyone who committed to it" : null,
      ].filter((who): who is string => who !== null);
      const notBy =
        forbidden.length > 0
          ? `The policy must stop ${joinWords(forbidden)} from approving a merge request.`
          : null;
      return {
        id,
        sentence: `Every merge request to a protected branch needs at least ${String(count)} ${count === 1 ? "approval" : "approvals"} from a GitLab approval policy`,
        detail: [notBy, description].filter(Boolean).join(" ") || null,
        severity,
      };
    }
    return { id, sentence: `Rule ${id}`, detail: text(rule.type) || null, severity };
  });
}

/** The groups a saved version is limited to, or null for every group. */
export function scopeGroups(document: unknown): string[] | null {
  if (!isRecord(document) || !isRecord(document.spec) || !isRecord(document.spec.scope)) {
    return null;
  }
  const groups = document.spec.scope.groups;
  if (!Array.isArray(groups)) return null;
  const paths = groups.filter((group): group is string => typeof group === "string");
  return paths.includes("*") ? null : paths;
}
