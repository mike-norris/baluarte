import { frameworkIds, type FrameworkId } from "../api/controls";
import type { ReportRequest } from "../api/reports";
import { fromLocal } from "../asof/format";
import { readGroups } from "./package";

/** The report form's fields as text. */
export interface ReportForm {
  framework: FrameworkId | "";
  /** ``datetime-local`` values, read as UTC. */
  from: string;
  to: string;
  groups: string;
}

/** The request the form describes, or the reasons it can't be sent. */
export function validateReport(
  form: ReportForm,
): { request: ReportRequest } | { errors: string[] } {
  const errors: string[] = [];
  const framework = frameworkIds.find((id) => id === form.framework);
  if (!framework) errors.push("Choose the framework the report is for.");
  const from = fromLocal(form.from);
  const to = fromLocal(form.to);
  if (!from) errors.push("Choose when the period starts.");
  if (!to) errors.push("Choose when the period ends.");
  if (from && to && Date.parse(from) >= Date.parse(to)) {
    errors.push("The period must end after it starts.");
  }
  const groups = readGroups(form.groups, errors);
  if (errors.length > 0 || !framework || !from || !to) return { errors };
  return {
    request: { framework, from, to, ...(groups.length > 0 ? { groups } : {}) },
  };
}
