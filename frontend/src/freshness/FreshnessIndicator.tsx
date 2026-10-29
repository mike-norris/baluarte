import { Link } from "react-router";

import { useFreshness, type FreshnessSignal } from "./context";
import { freshnessTone, needsAttention, statusLabel } from "./status";

function announcement({ source, status }: FreshnessSignal): string {
  if (source === "evaluation") {
    switch (status) {
      case "failed":
        return "The latest policy evaluation failed. Compliance results may be out of date.";
      case "stale":
        return "Compliance results are stale.";
      case "unknown":
        return "Policy evaluation status could not be checked.";
      default:
        return "";
    }
  }
  switch (status) {
    case "failed":
      return "The latest GitLab collection failed. GitLab data may be out of date.";
    case "stale":
      return "GitLab data is stale.";
    case "unknown":
      return "GitLab collection status could not be checked.";
    default:
      return "";
  }
}

/** Details for the signal that sets the badge: the inventory for collection, else the dashboard. */
const detailsPage: Record<FreshnessSignal["source"], string> = {
  collection: "/inventory",
  evaluation: "/",
};

/**
 * The header's freshness badge: the worst of collection and evaluation. The live region only
 * changes when the status does, so a failed or stale status is announced once rather than on
 * every navigation or re-check.
 */
export function FreshnessIndicator() {
  const freshness = useFreshness();
  if (!freshness) return null;
  const { overall, worstSignal } = freshness;
  return (
    <>
      <Link
        to={detailsPage[worstSignal.source]}
        className={`rounded border px-2 py-1 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${freshnessTone[overall]}`}
      >
        {`GitLab data: ${statusLabel(worstSignal.source, overall)}`}
      </Link>
      <p aria-live="polite" className="sr-only">
        {needsAttention(overall) ? announcement(worstSignal) : ""}
      </p>
    </>
  );
}
