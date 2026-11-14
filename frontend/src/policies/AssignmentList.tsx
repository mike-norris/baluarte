import { useState } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import { removeAssignment, type Assignment } from "../api/policies";
import { useAuth } from "../auth/context";
import { formatTime } from "../time";
import {
  baselineHref,
  canWritePolicies,
  proposedMessage,
  reviewText,
  targetText,
  targetTypeLabels,
} from "./format";

const cell = "align-top";
const dangerClass = "btn btn-danger btn-sm";

const stateNotes: Record<string, string> = {
  missing: "no longer found in GitLab",
  inaccessible: "not readable with Baluarte's token",
};

/**
 * Where one baseline is applied. Users with ``policy:write`` can stop checking it on a target,
 * after a confirmation that says what happens there instead.
 */
export function AppliedToList({
  assignments,
  latest,
  onChanged,
}: {
  assignments: Assignment[];
  /** The baseline's newest version, to mark places still on an older one. */
  latest: number;
  onChanged: (message: string) => void;
}) {
  const { session, refresh } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const canWrite = canWritePolicies(user);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [removing, setRemoving] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function remove(assignment: Assignment) {
    if (!user) return;
    setRemoving(true);
    setFailure(null);
    try {
      const proposed = await removeAssignment(assignment.id, user.csrf_token);
      setConfirming(null);
      onChanged(
        proposed
          ? proposedMessage(proposed)
          : `Stopped checking ${assignment.policy_name} on ${targetText(assignment)}. Checking again now.`,
      );
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setRemoving(false);
    }
  }

  if (assignments.length === 0) {
    return (
      <p className="text-secondary border border-dashed rounded p-3">
        Not applied anywhere yet, so nothing is checked against it.
      </p>
    );
  }

  return (
    <div>
      {failure && (
        <p role="alert" className="text-danger-emphasis">
          {failure}
        </p>
      )}
      <div className="card table-responsive">
        <table className="table table-vcenter card-table mb-0">
          <caption className="visually-hidden">Where this baseline is applied</caption>
          <thead>
            <tr>
              {["Applied to", "Kind", "Version", "Since"].map((header) => (
                <th key={header} scope="col" className={`${cell} font-medium`}>
                  {header}
                </th>
              ))}
              {canWrite && (
                <th scope="col" className={`${cell} font-medium`}>
                  <span className="visually-hidden">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {assignments.map((assignment) => {
              const where =
                assignment.target_type === "instance"
                  ? "Whole instance"
                  : (assignment.target_path ?? `GitLab ID ${String(assignment.target_id)}`);
              const note = assignment.target_state
                ? stateNotes[assignment.target_state]
                : undefined;
              return (
                <tr key={assignment.id}>
                  <th scope="row" className={`${cell} font-medium`}>
                    {where}
                    {note && (
                      <span className="d-block small fw-normal text-warning-emphasis">{note}</span>
                    )}
                  </th>
                  <td className={cell}>{targetTypeLabels[assignment.target_type]}</td>
                  <td className={cell}>
                    <Link to={baselineHref(assignment.policy_name, assignment.version)}>
                      {`v${String(assignment.version)}`}
                    </Link>
                    {assignment.version < latest && (
                      <span className="ms-2 small text-secondary">{`(v${String(latest)} is newer)`}</span>
                    )}
                  </td>
                  <td className={cell}>
                    <span className="text-nowrap">{formatTime(assignment.created_at)}</span>
                    <span className="d-block small text-secondary">
                      {reviewText(assignment.review)}
                    </span>
                  </td>
                  {canWrite && (
                    <td className={cell}>
                      {confirming === assignment.id ? (
                        <div>
                          <p>
                            {`Stop checking ${assignment.policy_name} on ${targetText(assignment)}? Projects there fall back to whatever is applied above it, or to nothing.`}
                          </p>
                          <div className="d-flex gap-2">
                            <button
                              type="button"
                              disabled={removing}
                              onClick={() => void remove(assignment)}
                              className={dangerClass}
                            >
                              {removing ? "Removing…" : "Remove"}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setConfirming(null);
                              }}
                              className="btn"
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      ) : (
                        <button
                          type="button"
                          onClick={() => {
                            setConfirming(assignment.id);
                            setFailure(null);
                          }}
                          aria-label={`Remove from ${where}`}
                          className="btn"
                        >
                          Remove from here
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
