/** Words, links and checks for label consistency: drift kinds, plan states and the taxonomy form. */

import { groupHref, projectHref } from "../api/gitlab";
import { NAME_PATTERN } from "../api/policies";
import {
  driftKinds,
  labelPlanStates,
  type DriftFilters,
  type DriftKind,
  type LabelOwner,
  type LabelPlanState,
  type LabelValues,
  type StepAction,
  type StepStatus,
  type TaxonomyLabel,
  type TaxonomySpec,
  type TaxonomyState,
} from "../api/labels";

export const LABELS = "/labels";
export const TAXONOMY_EDITOR = "/labels/taxonomy/new";
export const LABEL_PLANS = "/labels/plans";
export const LABEL_RULE_TYPE = "gitlab.labels.consistent";
/** Findings of the label rule, which show drift per project in posture and controls. */
export const LABEL_FINDINGS = `/findings?rule_type=${LABEL_RULE_TYPE}`;

export const driftKindLabels: Record<DriftKind, string> = {
  missing: "Missing",
  misplaced: "Misplaced",
  misnamed: "Misnamed",
  color: "Colour differs",
  description: "Description differs",
  duplicate: "Duplicate",
  unknown_label: "Not in the taxonomy",
  unknown: "Unknown",
};

export const driftKindMeaning: Record<DriftKind, string> = {
  missing: "A taxonomy label is not available here, nor on any group above.",
  misplaced: "Defined below the level the taxonomy places it at.",
  misnamed: "An alias or a case or spacing variant is used instead of the taxonomy's name.",
  color: "Defined with a different colour.",
  description: "Defined with a different description.",
  duplicate: "The same name is defined on more than one level of one hierarchy.",
  unknown_label: "A label the taxonomy does not list (strict taxonomies only).",
  unknown: "The labels here, or on a group above, could not be read.",
};

export const taxonomyStateLabels: Record<TaxonomyState, string> = {
  proposed: "Waiting for review",
  approved: "In force",
  rejected: "Rejected",
  superseded: "Superseded",
};

export const planStateLabels: Record<LabelPlanState, string> = {
  draft: "Draft",
  submitted: "Submitted",
  approved: "Approved",
  rejected: "Rejected",
  applying: "Applying",
  applied: "Applied",
  failed: "Failed",
  expired: "Expired",
};

export const planStateMeaning: Record<LabelPlanState, string> = {
  draft: "Its creator can review the steps and submit it for approval.",
  submitted: "Waiting for someone other than its creator to approve or reject it.",
  approved: "Approved for the digest shown. It can be applied until it expires.",
  rejected: "Rejected; nothing will be done with it.",
  applying: "Being applied in GitLab, one step at a time, each read back to verify it.",
  applied: "Every step was applied or already as intended, and verified.",
  failed: "Stopped at the first unexpected result. Later steps were not run.",
  expired: "Past its expiry; it can no longer be approved or applied.",
};

export const actionLabels: Record<StepAction, string> = {
  create: "Create label",
  rename: "Rename label",
  update: "Update label",
};

export const stepStatusLabels: Record<StepStatus, string> = {
  pending: "Not run yet",
  skipped: "Already as intended",
  applied: "Applied",
  failed: "Failed",
  not_run: "Not run",
};

export function isDriftKind(value: string | null): value is DriftKind {
  return driftKinds.some((kind) => kind === value);
}

export function isPlanState(value: string | null): value is LabelPlanState {
  return labelPlanStates.some((state) => state === value);
}

export function ownerHref(owner: LabelOwner): string {
  return `${LABELS}/owners/${owner.type}/${String(owner.gitlab_id)}`;
}

export function inventoryHref(owner: LabelOwner): string {
  return owner.type === "group" ? groupHref(owner.gitlab_id) : projectHref(owner.gitlab_id);
}

export function ownerText(owner: LabelOwner): string {
  return `${owner.type === "group" ? "Group" : "Project"} ${owner.path}`;
}

export function labelPlanHref(id: number): string {
  return `${LABEL_PLANS}/${String(id)}`;
}

const MAX_FILTER = 255;

function filterText(value: string | null): string | undefined {
  const text = value?.trim();
  return text && text.length <= MAX_FILTER ? text : undefined;
}

export function readDriftFilters(params: URLSearchParams): DriftFilters {
  const filters: DriftFilters = {};
  const group = filterText(params.get("group"));
  if (group) filters.group = group;
  const kind = params.get("kind");
  if (isDriftKind(kind)) filters.kind = kind;
  const label = filterText(params.get("label"));
  if (label) filters.label = label;
  return filters;
}

export function writeDriftFilters(filters: DriftFilters, base?: URLSearchParams): URLSearchParams {
  const params = new URLSearchParams(base);
  for (const name of ["group", "kind", "label"] as const) {
    const value = filters[name];
    if (value) params.set(name, value);
    else params.delete(name);
  }
  return params;
}

export function hasDriftFilters(filters: DriftFilters): boolean {
  return Boolean(filters.group || filters.kind || filters.label);
}

/** Label values as "name: x", "colour: #y" and "description: z" lines; empty when none. */
export function valuesText(values: LabelValues | null | undefined): string[] {
  if (!values) return [];
  const lines: string[] = [];
  if (values.name) lines.push(`name: ${values.name}`);
  if (values.color) lines.push(`colour: ${values.color}`);
  if (values.description !== undefined && values.description !== null) {
    lines.push(`description: ${values.description || "(empty)"}`);
  }
  return lines;
}

const HEX = /^#[0-9a-f]{6}$/i;

export function isHexColor(value: string): boolean {
  return HEX.test(value.trim());
}

/** Aliases from "P1, prio-high" or one per line: trimmed, blanks and repeats dropped. */
export function parseAliases(text: string): string[] {
  const aliases: string[] = [];
  for (const part of text.split(/[\n,]/)) {
    const alias = part.trim();
    if (alias && !aliases.includes(alias)) aliases.push(alias);
  }
  return aliases;
}

export const MAX_LABEL_NAME = 255;

/** The taxonomy form: the document's fields, with groups and aliases as typed. */
export interface TaxonomyForm {
  name: string;
  groups: string[];
  strict: boolean;
  labels: TaxonomyLabel[];
}

export function emptyTaxonomy(): TaxonomyForm {
  return { name: "", groups: ["*"], strict: false, labels: [] };
}

export function formFromSpec(name: string, spec: TaxonomySpec): TaxonomyForm {
  return {
    name,
    groups: [...spec.scope.groups],
    strict: spec.strict,
    labels: spec.labels.map((label) => ({ ...label, aliases: [...label.aliases] })),
  };
}

/** Mistakes the form can catch before the API validates the document. */
export function taxonomyProblems(form: TaxonomyForm): string[] {
  const problems: string[] = [];
  if (!NAME_PATTERN.test(form.name)) {
    problems.push(
      "Name the taxonomy with lowercase letters, digits and hyphens, starting and ending with a letter or digit (at most 63).",
    );
  }
  if (form.groups.length === 0) problems.push('Add at least one group pattern, or "*" for all.');
  if (form.labels.length === 0) problems.push("Add at least one label.");
  const seen = new Map<string, string>();
  for (const label of form.labels) {
    const name = label.name.trim();
    if (!name) problems.push("Every label needs a name.");
    else if (name.length > MAX_LABEL_NAME) {
      problems.push(`Label "${name}" is longer than ${String(MAX_LABEL_NAME)} characters.`);
    }
    if (!isHexColor(label.color)) {
      problems.push(`Label "${name || "(unnamed)"}" needs a colour like #1f75cb.`);
    }
    for (const used of [name, ...label.aliases]) {
      const key = used.trim().toLowerCase();
      if (!key) continue;
      const owner = seen.get(key);
      if (owner !== undefined && owner !== name) {
        problems.push(`"${used}" is used by both "${owner}" and "${name}".`);
      } else if (owner === name && used !== name) {
        problems.push(`"${used}" is listed twice for "${name}".`);
      }
      seen.set(key, name);
    }
  }
  return [...new Set(problems)];
}

/** A YAML string: JSON's double-quoted form is valid YAML. */
function quoted(value: string): string {
  return JSON.stringify(value);
}

/** The form as the YAML document the API validates and stores. */
export function taxonomyYaml(form: TaxonomyForm): string {
  const lines = [
    "apiVersion: baluarte.io/v1alpha1",
    "kind: LabelTaxonomy",
    "metadata:",
    `  name: ${quoted(form.name.trim())}`,
    "spec:",
    "  scope:",
    `    groups: [${form.groups.map(quoted).join(", ")}]`,
    "  placement: top_level_group",
    `  strict: ${form.strict ? "true" : "false"}`,
    form.labels.length > 0 ? "  labels:" : "  labels: []",
  ];
  for (const label of form.labels) {
    lines.push(`    - name: ${quoted(label.name.trim())}`);
    lines.push(`      color: ${quoted(label.color.trim().toLowerCase())}`);
    const description = label.description?.trim();
    if (description) lines.push(`      description: ${quoted(description)}`);
    if (label.aliases.length > 0) {
      lines.push(`      aliases: [${label.aliases.map(quoted).join(", ")}]`);
    }
  }
  return `${lines.join("\n")}\n`;
}
