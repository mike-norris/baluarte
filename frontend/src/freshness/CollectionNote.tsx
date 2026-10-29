import { Link } from "react-router";

import { formatTime } from "../time";
import { useFreshness } from "./context";
import { freshnessLabels } from "./status";

/** Collection status for pages whose content will be derived from GitLab data. */
export function CollectionNote() {
  const freshness = useFreshness();
  const collection = freshness?.signals.find((signal) => signal.source === "collection");
  if (!collection) return null;
  return (
    <aside aria-label="Data freshness" className="mt-6 space-y-1 text-sm text-slate-700">
      <p>
        GitLab collection status:{" "}
        <span className="font-medium">{freshnessLabels[collection.status]}</span>
      </p>
      {collection.lastSuccessAt && (
        <p>Oldest successful collection: {formatTime(collection.lastSuccessAt)}</p>
      )}
      <p>
        <Link to="/inventory" className="text-sky-800 underline">
          Collection details
        </Link>
      </p>
      <p>Policy evaluation is not available yet, so nothing on this page has been evaluated.</p>
    </aside>
  );
}
