/** Evidence gaps per control and period (roadmap M15.4). */

import { getJson } from "./client";

export type Gap = "missing" | "stale" | "inaccessible" | "incomplete";

export interface ControlGaps {
  control_id: string;
  title: string;
  area: string;
  status: string | null;
  gaps: Gap[];
  reasons: string[];
  accepted_items: number;
  fresh_items: number;
  collected_results: number;
  assessment_id: number | null;
  conclusion: string | null;
}

export interface GapReport {
  framework: string;
  version: string;
  period_start: string;
  period_end: string;
  evaluation_run_id: number | null;
  collected_at: string | null;
  summary: Record<Gap, number>;
  controls: ControlGaps[];
}

export function getEvidenceGaps(
  query: { framework: string; period_start: string; period_end: string },
  signal?: AbortSignal,
): Promise<GapReport> {
  return getJson("/api/v1/evidence-gaps", { ...query }, signal);
}

export const gapLabels: Record<Gap, string> = {
  missing: "Missing",
  stale: "Stale",
  inaccessible: "Inaccessible",
  incomplete: "Incomplete",
};
