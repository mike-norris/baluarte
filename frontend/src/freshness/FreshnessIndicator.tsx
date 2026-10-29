import { Link } from "react-router";

import { useFreshness } from "./context";
import { freshnessLabels, freshnessTone, needsAttention, type SignalStatus } from "./status";

function announcement(status: SignalStatus): string {
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

/**
 * The header's freshness badge. The live region only changes when the status does, so a
 * failed or stale status is announced once rather than on every navigation or re-check.
 */
export function FreshnessIndicator() {
  const freshness = useFreshness();
  if (!freshness) return null;
  const { overall } = freshness;
  return (
    <>
      <Link
        to="/inventory"
        className={`rounded border px-2 py-1 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${freshnessTone[overall]}`}
      >
        {`GitLab data: ${freshnessLabels[overall]}`}
      </Link>
      <p aria-live="polite" className="sr-only">
        {needsAttention(overall) ? announcement(overall) : ""}
      </p>
    </>
  );
}
