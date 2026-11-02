import { useState } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import { removeAssignment, type Assignment } from "../api/policies";
import { useAuth } from "../auth/context";
import { linkClass, secondaryClass } from "../components/styles";
import { formatTime } from "../time";
import {
  baselineHref,
  canWritePolicies,
  proposedMessage,
  reviewText,
  targetText,
  targetTypeLabels,
} from "./format";

const cell = "px-3 py-2";
const dangerClass =
  "rounded bg-red-800 px-3 py-2 text-sm font-medium text-white hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 disabled:opacity-50";

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
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        Not applied anywhere yet, so nothing is checked against it.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {failure && (
        <p role="alert" className="text-sm text-red-900">
          {failure}
        </p>
      )}
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Where this baseline is applied</caption>
          <thead className="border-b border-slate-200 bg-slate-50">
            <tr>
              {["Applied to", "Kind", "Version", "Since"].map((header) => (
                <th key={header} scope="col" className={`${cell} font-medium`}>
                  {header}
                </th>
              ))}
              {canWrite && (
                <th scope="col" className={`${cell} font-medium`}>
                  <span className="sr-only">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
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
                      <span className="block text-xs font-normal text-amber-900">{note}</span>
                    )}
                  </th>
                  <td className={cell}>{targetTypeLabels[assignment.target_type]}</td>
                  <td className={cell}>
                    <Link
                      to={baselineHref(assignment.policy_name, assignment.version)}
                      className={linkClass}
                    >
                      {`v${String(assignment.version)}`}
                    </Link>
                    {assignment.version < latest && (
                      <span className="ml-2 text-xs text-slate-600">{`(v${String(latest)} is newer)`}</span>
                    )}
                  </td>
                  <td className={cell}>
                    <span className="whitespace-nowrap">{formatTime(assignment.created_at)}</span>
                    <span className="block text-xs text-slate-600">
                      {reviewText(assignment.review)}
                    </span>
                  </td>
                  {canWrite && (
                    <td className={cell}>
                      {confirming === assignment.id ? (
                        <div className="space-y-2">
                          <p className="text-sm">
                            {`Stop checking ${assignment.policy_name} on ${targetText(assignment)}? Projects there fall back to whatever is applied above it, or to nothing.`}
                          </p>
                          <div className="flex gap-2">
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
                              className={secondaryClass}
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
                          className={secondaryClass}
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
