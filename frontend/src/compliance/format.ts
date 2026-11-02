import type {
  ComplianceSettings,
  ControlStatus,
  FrameworkId,
  Requirement,
  SettingName,
} from "../api/compliance";

export const COMPLIANCE = "/compliance";

export const frameworks: { id: FrameworkId; name: string; short: string }[] = [
  { id: "soc2", name: "SOC 2 Type II", short: "SOC 2" },
  { id: "iso27001", name: "ISO/IEC 27001:2022", short: "ISO 27001" },
  {
    id: "nist_800_53_moderate",
    name: "NIST SP 800-53 Rev 5 Moderate",
    short: "NIST 800-53",
  },
];

export function frameworkName(id: FrameworkId): string {
  return frameworks.find((item) => item.id === id)?.name ?? id;
}

export function frameworkShort(id: FrameworkId): string {
  return frameworks.find((item) => item.id === id)?.short ?? id;
}

export function frameworkList(ids: readonly FrameworkId[]): string {
  return ids.length > 0 ? ids.map(frameworkName).join(", ") : "None (standard defaults)";
}

/**
 * What each framework requires of each setting. Used to preview the
 * effect of a choice before it is saved, including during setup, where the catalogue cannot be
 * read; the API enforces the same values and is the authority.
 */
export const frameworkRequirements: Record<
  FrameworkId,
  Partial<Record<SettingName, Requirement>>
> = {
  soc2: {
    audit_retention_days: { op: ">=", value: 365 },
    evidence_retention_days: { op: ">=", value: 365 },
    session_idle_minutes: { op: "<=", value: 30 },
    session_lifetime_minutes: { op: "<=", value: 720 },
    require_review: { op: "==", value: true },
  },
  iso27001: {
    audit_retention_days: { op: ">=", value: 365 },
    evidence_retention_days: { op: ">=", value: 365 },
    session_idle_minutes: { op: "<=", value: 30 },
    session_lifetime_minutes: { op: "<=", value: 720 },
    require_review: { op: "==", value: true },
  },
  nist_800_53_moderate: {
    audit_retention_days: { op: ">=", value: 365 },
    evidence_retention_days: { op: ">=", value: 365 },
    session_idle_minutes: { op: "<=", value: 15 },
    session_lifetime_minutes: { op: "<=", value: 720 },
    require_review: { op: "==", value: true },
  },
};

export const defaultSettings: ComplianceSettings = {
  audit_retention_days: 365,
  evidence_retention_days: 365,
  job_retention_days: 90,
  session_idle_minutes: 60,
  session_lifetime_minutes: 480,
  require_review: false,
  legal_hold: false,
};

export interface Strictest extends Requirement {
  /** The selected frameworks that impose this value. */
  frameworks: FrameworkId[];
}

/** The strictest requirement the selected frameworks place on a setting, or null for none. */
export function strictest(
  selected: readonly FrameworkId[],
  setting: SettingName,
): Strictest | null {
  let best: Strictest | null = null;
  for (const id of selected) {
    const requirement = frameworkRequirements[id][setting];
    if (!requirement) continue;
    if (!best || stricter(requirement, best)) {
      best = { ...requirement, frameworks: [id] };
    } else if (requirement.value === best.value) {
      best.frameworks.push(id);
    }
  }
  return best;
}

function stricter(candidate: Requirement, current: Requirement): boolean {
  if (candidate.op === ">=") return Number(candidate.value) > Number(current.value);
  if (candidate.op === "<=") return Number(candidate.value) < Number(current.value);
  return false;
}

export function meets(value: number | boolean, requirement: Requirement): boolean {
  if (requirement.op === ">=") return Number(value) >= Number(requirement.value);
  if (requirement.op === "<=") return Number(value) <= Number(requirement.value);
  return value === requirement.value;
}

/** Raise or lower each constrained setting just enough to meet the selected frameworks. */
export function applyMinimums(
  settings: ComplianceSettings,
  selected: readonly FrameworkId[],
  locked: (setting: SettingName) => boolean = () => false,
): ComplianceSettings {
  const next = { ...settings };
  for (const setting of Object.keys(next) as SettingName[]) {
    const requirement = strictest(selected, setting);
    if (!requirement || locked(setting) || meets(next[setting], requirement)) continue;
    (next as Record<SettingName, number | boolean>)[setting] = requirement.value;
  }
  return next;
}

export interface SettingInfo {
  label: string;
  unit?: "days" | "minutes";
  help: string;
}

export const settingInfo: Record<SettingName, SettingInfo> = {
  audit_retention_days: {
    label: "Keep the audit log for",
    unit: "days",
    help: "Who did what in Baluarte. Older events are deleted by the daily retention job.",
  },
  evidence_retention_days: {
    label: "Keep evidence for",
    unit: "days",
    help: "Observations and evaluation results. The latest state of each project and anything a finding, exception or plan still uses are always kept.",
  },
  job_retention_days: {
    label: "Keep job and run history for",
    unit: "days",
    help: "Collection jobs, collection runs and evidence exports.",
  },
  session_idle_minutes: {
    label: "Sign out after inactivity of",
    unit: "minutes",
    help: "A session ends after this long without activity.",
  },
  session_lifetime_minutes: {
    label: "Sign out after at most",
    unit: "minutes",
    help: "A session ends after this long even if it is in use.",
  },
  require_review: {
    label: "Require a second person to approve baseline changes",
    help: "A new baseline version, or a change to where a baseline applies, takes effect only after someone else approves it.",
  },
  legal_hold: {
    label: "Legal hold: delete nothing",
    help: "Stops the retention job from deleting anything, for example during an investigation or litigation.",
  },
};

export const settingOrder: SettingName[] = [
  "audit_retention_days",
  "evidence_retention_days",
  "job_retention_days",
  "session_idle_minutes",
  "session_lifetime_minutes",
  "require_review",
  "legal_hold",
];

/** "At least 365 days", "At most 15 minutes", "Required". */
export function describeRequirement(setting: SettingName, requirement: Requirement): string {
  if (requirement.op === "==") return requirement.value === true ? "Required" : "Must be off";
  const unit = settingInfo[setting].unit ?? "";
  const amount = `${String(requirement.value)} ${unit}`.trim();
  return requirement.op === ">=" ? `At least ${amount}` : `At most ${amount}`;
}

export const statusLabel: Record<ControlStatus, string> = {
  met: "Met",
  not_met: "Not met",
  needs_attestation: "Needs attestation",
  excepted: "Exception in force",
  not_applicable: "Not applicable",
};

export const statusClass: Record<ControlStatus, string> = {
  met: "bg-emerald-100 text-emerald-900",
  not_met: "bg-red-100 text-red-900",
  needs_attestation: "bg-amber-100 text-amber-950",
  excepted: "bg-sky-100 text-sky-900",
  not_applicable: "bg-slate-100 text-slate-700",
};

export const statusOrder: ControlStatus[] = [
  "not_met",
  "needs_attestation",
  "excepted",
  "met",
  "not_applicable",
];

/** An ISO date ``days`` after ``from`` (UTC). */
export function isoDate(from: Date, days: number): string {
  const date = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()));
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}
