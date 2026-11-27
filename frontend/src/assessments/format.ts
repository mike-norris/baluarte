import type { Conclusion } from "../api/assessments";
import type { Tone } from "../ui/Badge";

export const conclusionTones: Record<Conclusion, Tone> = {
  effective: "success",
  partially_effective: "warning",
  not_effective: "danger",
  not_tested: "secondary",
};
