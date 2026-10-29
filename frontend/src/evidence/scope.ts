import { outcomes, type FindingFilters, type FindingState, type Outcome } from "../api/evaluations";

export type ExportFormat = "json" | "csv";

/** The API's export scope: only the fields that narrow it, or ``all`` alone. */
export interface ExportScope {
  all?: true;
  group?: string;
  projects?: number[];
  policy?: string;
  rule?: string;
  outcome?: Outcome;
  finding_state?: FindingState;
  since?: string;
  until?: string;
}

export interface ExportRequest {
  format: ExportFormat;
  scope: ExportScope;
}

/** The export form's fields as text, as read from the URL or the form. */
export interface ExportForm {
  all: boolean;
  group: string;
  projects: string;
  policy: string;
  rule: string;
  outcome: Outcome | "";
  findingState: FindingState | "";
  /** ``YYYY-MM-DDTHH:MM`` in UTC, as a ``datetime-local`` input holds it. */
  since: string;
  until: string;
  format: ExportFormat;
}

export const findingStates: readonly FindingState[] = ["open", "resolved", "excepted"];

// Limits enforced by the API; checked here so mistakes are explained before a request.
const MAX_NAME = 63;
const MAX_GROUP = 255;
const MAX_PROJECTS = 500;
const LOCAL_TIME = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?Z?$/;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | "" {
  return allowed.find((item) => item === value) ?? "";
}

/** A URL time (``2026-10-01T00:00:00Z``) as a ``datetime-local`` value, or "" when invalid. */
function toLocal(value: string | null): string {
  const match = LOCAL_TIME.exec(value?.trim() ?? "");
  return match?.[1] ?? "";
}

export function readExportForm(params: URLSearchParams): ExportForm {
  return {
    all: params.get("all") === "true",
    group: params.get("group")?.trim() ?? "",
    projects: (params.get("projects") ?? "")
      .split(/[\s,]+/)
      .filter(Boolean)
      .join(", "),
    policy: params.get("policy")?.trim() ?? "",
    rule: params.get("rule")?.trim() ?? "",
    outcome: oneOf<Outcome>(params.get("outcome"), outcomes),
    findingState: oneOf(params.get("finding_state"), findingStates),
    since: toLocal(params.get("since")),
    until: toLocal(params.get("until")),
    format: params.get("format") === "csv" ? "csv" : "json",
  };
}

/** The URL query that prefills the export form; links to "export this view" use it. */
export function writeExportParams(form: Partial<ExportForm>): URLSearchParams {
  const params = new URLSearchParams();
  if (form.all) params.set("all", "true");
  if (form.group) params.set("group", form.group);
  if (form.projects)
    params.set(
      "projects",
      form.projects
        .split(/[\s,]+/)
        .filter(Boolean)
        .join(","),
    );
  if (form.policy) params.set("policy", form.policy);
  if (form.rule) params.set("rule", form.rule);
  if (form.outcome) params.set("outcome", form.outcome);
  if (form.findingState) params.set("finding_state", form.findingState);
  if (form.since) params.set("since", `${form.since}:00Z`);
  if (form.until) params.set("until", `${form.until}:00Z`);
  if (form.format === "csv") params.set("format", "csv");
  return params;
}

export function evidenceHref(form: Partial<ExportForm>): string {
  const query = writeExportParams(form).toString();
  return query ? `/evidence?${query}` : "/evidence";
}

/**
 * The export link for a findings view. Findings filters map onto export scope fields, except
 * severity, which the export cannot filter by; ``dropped`` names it so the page can say so.
 */
export function findingsExportLink(filters: FindingFilters): {
  href: string;
  dropped: string[];
} {
  return {
    href: evidenceHref({
      findingState: filters.state ?? "",
      outcome: filters.outcome ?? "",
      projects: filters.project === undefined ? "" : String(filters.project),
      policy: filters.policy ?? "",
      rule: filters.rule ?? "",
    }),
    dropped: filters.severity ? ["severity"] : [],
  };
}

export type Validated = { request: ExportRequest } | { errors: string[] };

/** Check the form the way the API will, and build the request body with only the set fields. */
export function validateExport(form: ExportForm): Validated {
  if (form.all) return { request: { format: form.format, scope: { all: true } } };
  const errors: string[] = [];
  const scope: ExportScope = {};

  const group = form.group.trim();
  if (group) {
    if (group.startsWith("/") || group.endsWith("/")) {
      errors.push("Group path: give the full path without leading or trailing slashes.");
    } else if (group.length > MAX_GROUP) {
      errors.push(`Group path: at most ${String(MAX_GROUP)} characters.`);
    } else {
      scope.group = group;
    }
  }

  const ids = form.projects.split(/[\s,]+/).filter(Boolean);
  if (ids.length > 0) {
    const numbers = ids.map(Number);
    if (numbers.some((id) => !Number.isSafeInteger(id) || id <= 0)) {
      errors.push("Project GitLab IDs: use whole numbers above zero, separated by commas.");
    } else {
      const unique = [...new Set(numbers)];
      if (unique.length > MAX_PROJECTS) {
        errors.push(`Project GitLab IDs: at most ${String(MAX_PROJECTS)} projects.`);
      } else {
        scope.projects = unique;
      }
    }
  }

  for (const [field, label] of [
    ["policy", "Policy name"],
    ["rule", "Rule ID"],
  ] as const) {
    const value = form[field].trim();
    if (value.length > MAX_NAME) errors.push(`${label}: at most ${String(MAX_NAME)} characters.`);
    else if (value) scope[field] = value;
  }

  if (form.outcome) scope.outcome = form.outcome;
  if (form.findingState) scope.finding_state = form.findingState;

  for (const field of ["since", "until"] as const) {
    if (!form[field]) continue;
    if (toLocal(form[field]) === "") errors.push(`Choose a valid ${field} time.`);
    else scope[field] = `${toLocal(form[field])}:00Z`;
  }
  if (scope.since && scope.until && scope.since >= scope.until) {
    errors.push("The start of the time window must be before its end.");
  }

  if (errors.length === 0 && Object.keys(scope).length === 0) {
    errors.push("Choose at least one scope field, or tick “Export everything”.");
  }
  return errors.length > 0 ? { errors } : { request: { format: form.format, scope } };
}

/**
 * A safe file name from the server's ``Content-Disposition``: no path, only letters, digits,
 * dots, hyphens and underscores, and the extension of the requested format.
 */
export function exportFilename(disposition: string | null, format: ExportFormat): string {
  const extension = format === "json" ? ".json" : ".zip";
  const quoted = /filename="([^"]*)"/i.exec(disposition ?? "");
  const bare = /filename=([^;\s]+)/i.exec(disposition ?? "");
  const raw = quoted?.[1] ?? bare?.[1] ?? "";
  const base = raw.split(/[\\/]/).at(-1) ?? "";
  const name = base
    .replace(/[^A-Za-z0-9._-]/g, "_")
    .replace(/^[._]+/, "")
    .slice(0, 120);
  if (!name) return `baluarte-evidence${extension}`;
  return name.toLowerCase().endsWith(extension) ? name : `${name}${extension}`;
}
