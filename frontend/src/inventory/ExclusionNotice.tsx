import { useState } from "react";

import { includableHere, listExclusions, monitoringLabel, type Monitoring } from "../api/scope";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { PastReadOnly } from "../asof/PastReadOnly";
import { useAuth } from "../auth/context";
import { changePerson } from "../controls/format";
import { canWritePolicies } from "../policies/format";
import { formatTime } from "../time";
import { ScopeForm } from "./ScopeForm";

/**
 * Why a project or group is out of monitoring: how it is excluded, the justification, and, where
 * the exclusion names this very target, a way to include it again.
 */
export function ExclusionNotice({
  monitoring,
  type,
  path,
  onChanged,
}: {
  monitoring: Monitoring | undefined;
  type: "project" | "group";
  path: string;
  onChanged: (message: string) => void;
}) {
  const { session } = useAuth();
  const asOf = useAsOf();
  const canWrite = canWritePolicies(session.status === "signed_in" ? session.user : null);
  const [including, setIncluding] = useState(false);
  const excluded = monitoring?.excluded === true;
  const exclusions = useLoad(`scope:active:${type}:${path}`, (signal) =>
    excluded ? listExclusions("active", signal) : Promise.resolve({ items: [] }),
  );
  if (!excluded) return null;
  const exclusion = exclusions.data?.items.find((item) => item.id === monitoring.exclusion_id);
  const here = includableHere(monitoring, type, path);
  return (
    <section aria-labelledby="excluded-title" className="alert alert-secondary text-body">
      <h2 id="excluded-title" className="card-title">
        Excluded from monitoring
      </h2>
      <p>
        {`${monitoringLabel(monitoring, type, path) ?? "Excluded"}. It stays in the inventory but is not evaluated, read in detail or counted.`}
      </p>
      {exclusion && (
        <>
          <p className="text-prewrap">{exclusion.justification}</p>
          <p className="small text-secondary">
            {`Excluded by ${changePerson(exclusion.proposed_by)} on ${formatTime(exclusion.proposed_at)}.`}
          </p>
        </>
      )}
      {canWrite && !here && (
        <p className="text-secondary">
          Include it again from the Monitoring scope section of the Inventory page, where the
          covering exclusion is listed.
        </p>
      )}
      {canWrite && here && asOf !== null && <PastReadOnly />}
      {canWrite && here && !including && (
        <button
          type="button"
          disabled={asOf !== null}
          onClick={() => {
            setIncluding(true);
          }}
          className="btn"
        >
          Include again
        </button>
      )}
      {including && asOf === null && monitoring.exclusion_id !== null && (
        <ScopeForm
          intent={{ mode: "include", ids: [monitoring.exclusion_id], labels: [path] }}
          onCancel={() => {
            setIncluding(false);
          }}
          onDone={(text) => {
            setIncluding(false);
            onChanged(text);
          }}
        />
      )}
    </section>
  );
}
