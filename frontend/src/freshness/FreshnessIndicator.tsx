import { Link } from "react-router";

import { useFreshness, type FreshnessSignal } from "./context";
import { freshnessColor, needsAttention, statusLabel } from "./status";

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
  const label = `GitLab data: ${statusLabel(worstSignal.source, overall)}`;
  return (
    <>
      <Link
        to={detailsPage[worstSignal.source]}
        aria-label={label}
        className={`status status-${freshnessColor[overall]} status-lite text-decoration-none`}
      >
        <span className="status-dot" aria-hidden="true" />
        <span className="d-none d-md-inline">{label}</span>
      </Link>
      <p aria-live="polite" className="visually-hidden">
        {needsAttention(overall) ? announcement(worstSignal) : ""}
      </p>
    </>
  );
}
