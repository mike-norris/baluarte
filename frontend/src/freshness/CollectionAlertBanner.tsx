import { Link } from "react-router";

import { useAuth } from "../auth/context";
import { formatTime } from "../time";
import { useFreshness } from "./context";

/**
 * Shown on every signed-in page while GitLab collection is unhealthy, so a failing or stalled
 * collection is noticed instead of surfacing only as a status in the header.
 */
export function CollectionAlertBanner() {
  const freshness = useFreshness();
  const { session } = useAuth();
  const alert = freshness?.report?.alert;
  if (!alert) return null;
  const canManage =
    session.status === "signed_in" && session.user.permissions.includes("settings:manage");
  const lastSuccess = alert.last_success_at
    ? `The last successful collection was ${formatTime(alert.last_success_at)}.`
    : "Collection has not succeeded yet.";
  return (
    <div role="alert" className="alert alert-danger rounded-0 border-start-0 border-end-0 mb-0">
      <div className="container-xl">
        <p className="alert-title">
          {alert.reason === "failing"
            ? `GitLab collection has failed ${String(alert.consecutive_failures)} times in a row` +
              (alert.failing_since ? ` since ${formatTime(alert.failing_since)}.` : ".")
            : "GitLab collection is overdue."}
        </p>
        <p className="mb-1">
          {lastSuccess} Results are not current until collection succeeds.{" "}
          {alert.reason === "failing"
            ? "Check that Baluarte can reach GitLab and that the token is valid."
            : "Check that the Baluarte worker is running."}
        </p>
        {alert.last_error && <p className="mb-1">Last error: {alert.last_error}</p>}
        <p className="mb-0">
          {canManage ? (
            <Link to="/settings">
              Check the GitLab connection in Settings
            </Link>
          ) : (
            "Tell your Baluarte administrator."
          )}
          {" · "}
          <Link to="/inventory">
            Collection details
          </Link>
        </p>
      </div>
    </div>
  );
}
