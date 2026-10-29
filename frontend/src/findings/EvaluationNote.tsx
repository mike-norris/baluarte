import { useFreshness } from "../freshness/context";
import { freshnessTone, needsAttention, statusLabel } from "../freshness/status";
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
    <div className={`rounded border p-3 text-sm ${freshnessTone[signal.status]}`}>
      <p className="font-medium">{`Policy evaluation: ${statusLabel("evaluation", signal.status)}`}</p>
      <p>{`Last successful evaluation: ${formatTime(signal.lastSuccessAt)}.`}</p>
      {needsAttention(signal.status) && (
        <p>These results are not current compliance until an evaluation succeeds again.</p>
      )}
      {report?.behind_collection && (
        <p>GitLab data was collected after the last evaluation started; results may lag it.</p>
      )}
    </div>
  );
}
