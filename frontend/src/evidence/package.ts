import { frameworkIds, type FrameworkId } from "../api/controls";
import { MAX_SAMPLE, type PackageRequest } from "../api/packages";
import { fromLocal } from "../asof/format";

/** The package form's fields as text. */
export interface PackageForm {
  /** ``datetime-local`` values, read as UTC. */
  from: string;
  to: string;
  framework: FrameworkId | "";
  control: string;
  groups: string;
  size: string;
  seed: string;
}

export const DEFAULT_SAMPLE = 25;
const MAX_GROUPS = 100;
const MAX_CONTROL = 20;
const MAX_GROUP = 255;

/** The request the form describes, or the reasons it can't be sent. */
export function validatePackage(
  form: PackageForm,
): { request: PackageRequest } | { errors: string[] } {
  const errors: string[] = [];
  const from = fromLocal(form.from);
  const to = fromLocal(form.to);
  if (!from) errors.push("Choose when the period starts.");
  if (!to) errors.push("Choose when the period ends.");
  if (from && to && Date.parse(from) >= Date.parse(to)) {
    errors.push("The period must end after it starts.");
  }
  const control = form.control.trim();
  const framework = frameworkIds.find((id) => id === form.framework);
  if (control && !framework) errors.push("Choose the framework the control belongs to.");
  if (control.length > MAX_CONTROL) errors.push("A control ID is at most 20 characters.");
  const groups = form.groups
    .split(/[\s,]+/)
    .map((group) => group.trim())
    .filter(Boolean);
  if (groups.length > MAX_GROUPS) errors.push("Name at most 100 groups.");
  if (groups.some((group) => group.length > MAX_GROUP)) {
    errors.push("A group path is at most 255 characters.");
  }
  const size = Number(form.size.trim() || String(DEFAULT_SAMPLE));
  if (!Number.isInteger(size) || size < 1 || size > MAX_SAMPLE) {
    errors.push(`The sample size must be a whole number from 1 to ${String(MAX_SAMPLE)}.`);
  }
  const seedText = form.seed.trim();
  const seed = Number(seedText);
  if (seedText && (!/^\d+$/.test(seedText) || !Number.isSafeInteger(seed))) {
    errors.push("The seed must be a whole number, or left empty to have one chosen.");
  }
  if (errors.length > 0 || !from || !to) return { errors };
  return {
    request: {
      from,
      to,
      ...(framework ? { framework } : {}),
      ...(control ? { control_id: control } : {}),
      ...(groups.length > 0 ? { groups } : {}),
      sample: seedText ? { size, seed } : { size },
    },
  };
}
