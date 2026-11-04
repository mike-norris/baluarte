import { useState } from "react";

import { listExclusions, targetText, type Exclusion } from "../api/scope";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { changePerson } from "../controls/format";
import { ErrorMessage } from "../components/ErrorMessage";
import { secondaryClass } from "../components/styles";
import { canWritePolicies } from "../policies/format";
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
    <li className="rounded border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="text-sm">
          <span className="font-medium">{what}</span>{" "}
          {proposed && (
            <span className="rounded bg-amber-100 px-1 text-xs text-amber-950">
              Waiting for review
            </span>
          )}
          <p className="mt-1 whitespace-pre-wrap text-slate-700">{exclusion.justification}</p>
          <p className="mt-1 text-xs text-slate-600">
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
            className={secondaryClass}
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
  const canWrite = canWritePolicies(user);
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
      <p role="status" className="text-slate-600">
        Loading exclusions…
      </p>
    );
  } else {
    const items = [...(proposed.data?.items ?? []), ...active.data.items];
    const personal = items.some((item) => item.target_type === "personal_namespaces");
    body = (
      <>
        {items.length === 0 ? (
          <p className="text-sm text-slate-600">
            Everything in the inventory is monitored. Select projects or groups below to exclude
            them.
          </p>
        ) : (
          <ul className="space-y-2" aria-label="Exclusions">
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
            className={secondaryClass}
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
    <section aria-labelledby="scope-title" className="space-y-3">
      <h2 id="scope-title" className="text-lg font-semibold">
        Monitoring scope
      </h2>
      <p className="text-sm text-slate-600">
        Excluded projects and groups stay in the inventory, so it still shows everything the token
        can see, but they are not evaluated, read in detail or counted.
      </p>
      {body}
    </section>
  );
}
