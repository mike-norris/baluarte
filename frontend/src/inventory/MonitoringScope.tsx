import { useState } from "react";

import { listExclusions, targetText, type Exclusion } from "../api/scope";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { changePerson } from "../controls/format";
import { ErrorMessage } from "../components/ErrorMessage";
import { canWritePolicies } from "../policies/format";
import { useDeviceClass } from "../ui/device";
import { formatTime } from "../time";
import { ScopeForm, type ScopeIntent } from "./ScopeForm";

function ExclusionItem({
  exclusion,
  canWrite,
  open,
}: {
  exclusion: Exclusion;
  canWrite: boolean;
  open: (intent: ScopeIntent) => void;
}) {
  const what = targetText(exclusion);
  const proposed = exclusion.state === "proposed";
  return (
    <li className="rounded border p-3">
      <div className="d-flex flex-wrap align-items-start justify-content-between gap-2">
        <div>
          <span className="fw-medium">{what}</span>{" "}
          {proposed && (
            <span className="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle">
              Waiting for review
            </span>
          )}
          <p className="mt-1 text-prewrap text-secondary">{exclusion.justification}</p>
          <p className="mt-1 small text-secondary">
            {`${proposed ? "Proposed" : "Excluded"} by ${changePerson(exclusion.proposed_by)} on ${formatTime(exclusion.proposed_at)}`}
            {!proposed &&
              (exclusion.self_approved
                ? "; not reviewed, review was off."
                : `; approved by ${changePerson(exclusion.decided_by)}.`)}
          </p>
        </div>
        {canWrite && !proposed && (
          <button
            type="button"
            onClick={() => {
              open({ mode: "include", ids: [exclusion.id], labels: [what] });
            }}
            className="btn"
          >
            {`Include again: ${what}`}
          </button>
        )}
      </div>
    </li>
  );
}

/**
 * What is out of monitoring and why: exclusions in force and waiting for review, with actions to
 * include a target again and to exclude every personal-namespace project.
 */
export function MonitoringScope({
  reload,
  onChanged,
}: {
  reload: number;
  onChanged: (message: string) => void;
}) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const device = useDeviceClass();
  // Phones read the scope; proposing a change needs a larger screen.
  const canWrite = canWritePolicies(user) && device !== "phone";
  const [intent, setIntent] = useState<ScopeIntent | null>(null);
  const active = useLoad(`scope:active#${String(reload)}`, (signal) =>
    listExclusions("active", signal),
  );
  const proposed = useLoad(`scope:proposed#${String(reload)}`, (signal) =>
    listExclusions("proposed", signal),
  );

  let body;
  if (active.error) body = <ErrorMessage error={active.error} retry={active.retry} />;
  else if (!active.data) {
    body = (
      <p role="status" className="text-secondary">
        Loading exclusions…
      </p>
    );
  } else {
    const items = [...(proposed.data?.items ?? []), ...active.data.items];
    const personal = items.some((item) => item.target_type === "personal_namespaces");
    body = (
      <>
        {items.length === 0 ? (
          <p className="text-secondary">
            Everything in the inventory is monitored. Select projects or groups below to exclude
            them.
          </p>
        ) : (
          <ul aria-label="Exclusions">
            {items.map((exclusion) => (
              <ExclusionItem
                key={exclusion.id}
                exclusion={exclusion}
                canWrite={canWrite}
                open={setIntent}
              />
            ))}
          </ul>
        )}
        {canWrite && !personal && intent === null && (
          <button
            type="button"
            onClick={() => {
              setIntent({
                mode: "exclude",
                targets: [],
                personalNamespaces: true,
                labels: ["All personal-namespace projects, including ones discovered later"],
              });
            }}
            className="btn"
          >
            Exclude all personal-namespace projects
          </button>
        )}
        {intent && (
          <ScopeForm
            intent={intent}
            onCancel={() => {
              setIntent(null);
            }}
            onDone={(message) => {
              setIntent(null);
              onChanged(message);
            }}
          />
        )}
      </>
    );
  }

  return (
    <section aria-labelledby="scope-title">
      <h2 id="scope-title" className="card-title">
        Monitoring scope
      </h2>
      <p className="text-secondary">
        Excluded projects and groups stay in the inventory, so it still shows everything the token
        can see, but they are not evaluated, read in detail or counted.
      </p>
      {body}
    </section>
  );
}
