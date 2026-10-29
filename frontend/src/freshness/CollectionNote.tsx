import { Link } from "react-router";

import { formatTime } from "../time";
import { useFreshness } from "./context";
import { statusLabel } from "./status";

/** Collection and evaluation status for pages whose content is derived from GitLab data. */
export function CollectionNote() {
  const freshness = useFreshness();
  const collection = freshness?.signals.find((signal) => signal.source === "collection");
  const evaluation = freshness?.signals.find((signal) => signal.source === "evaluation");
  if (!freshness || !collection || !evaluation) return null;
  return (
    <aside aria-label="Data freshness" className="mt-6 space-y-1 text-sm text-slate-700">
      <p>
        GitLab collection status:{" "}
        <span className="font-medium">{statusLabel("collection", collection.status)}</span>
      </p>
      {collection.lastSuccessAt && (
        <p>Oldest successful collection: {formatTime(collection.lastSuccessAt)}</p>
      )}
      <p>
        Policy evaluation status:{" "}
        <span className="font-medium">{statusLabel("evaluation", evaluation.status)}</span>
      </p>
      {evaluation.lastSuccessAt && (
        <p>Last successful evaluation: {formatTime(evaluation.lastSuccessAt)}</p>
      )}
      {freshness.evaluation?.behind_collection && (
        <p>
          GitLab data was collected after the last successful evaluation, so results may not reflect
          it yet.
        </p>
      )}
      <p>
        <Link to="/inventory" className="text-sky-800 underline">
          Collection details
        </Link>
        {" · "}
        <Link to="/" className="text-sky-800 underline">
          Compliance overview
        </Link>
      </p>
    </aside>
  );
}
