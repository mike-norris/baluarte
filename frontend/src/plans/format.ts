import type { LabelPlanState, StepStatus } from "../api/labels";
import type { Tone } from "../ui/Badge";

/** Label and issue plans share these states and step results. */
export const planStateTone: Record<LabelPlanState, Tone> = {
  draft: "secondary",
  submitted: "info",
  approved: "primary",
  rejected: "danger",
  applying: "primary",
  applied: "success",
  failed: "danger",
  expired: "secondary",
};

export const stepStatusTone: Record<StepStatus, Tone> = {
  pending: "secondary",
  skipped: "info",
  applied: "success",
  failed: "danger",
  not_run: "warning",
};

/** The lifecycle shown above a plan, ending where this plan ended. */
export function planLifecycle(state: LabelPlanState): { states: string[]; current: string } {
  const path = ["Draft", "Submitted", "Approved", "Applying", "Applied"];
  switch (state) {
    case "rejected":
      return { states: ["Draft", "Submitted", "Rejected"], current: "Rejected" };
    case "expired":
      return { states: ["Draft", "Submitted", "Expired"], current: "Expired" };
    case "failed":
      return { states: [...path.slice(0, 4), "Failed"], current: "Failed" };
    default: {
      const current = state.charAt(0).toUpperCase() + state.slice(1);
      return { states: path, current };
    }
  }
}
