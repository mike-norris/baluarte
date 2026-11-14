import { useFreshness } from "../freshness/context";
import { freshnessColor, needsAttention, statusLabel } from "../freshness/status";
import { formatTime } from "../time";

/**
 * Whether policy evaluation is current, so results are never read as current compliance when it
 * is not. The header badge announces changes; this note is static text.
 */
export function EvaluationNote() {
  const freshness = useFreshness();
  const signal = freshness?.signals.find((entry) => entry.source === "evaluation");
  if (!signal) return null;
  const report = freshness?.evaluation;
  return (
    <div className={`alert alert-${freshnessColor[signal.status]} text-body`}>
      <p className="fw-medium mb-1">{`Policy evaluation: ${statusLabel("evaluation", signal.status)}`}</p>
      <p className="mb-0">{`Last successful evaluation: ${formatTime(signal.lastSuccessAt)}.`}</p>
      {needsAttention(signal.status) && (
        <p className="mb-0 mt-1">
          These results are not current compliance until an evaluation succeeds again.
        </p>
      )}
      {report?.behind_collection && (
        <p className="mb-0 mt-1">
          GitLab data was collected after the last evaluation started; results may lag it.
        </p>
      )}
    </div>
  );
}
