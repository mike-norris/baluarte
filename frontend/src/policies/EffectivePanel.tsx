import { useState } from "react";
import { Link } from "react-router";

import { effectivePolicies, type Resolution } from "../api/policies";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { linkClass } from "../components/styles";
import { baselineHref, gapReasons, outcomeLabels, targetTypeLabels } from "./format";
import { TargetPicker, type Picked } from "./TargetPicker";

function where(step: { target_type: string; path: string }): string {
  return step.target_type === "instance" ? "This instance" : step.path;
}

function Result({ picked, version }: { picked: Picked; version: number }) {
  const loaded = useLoad(
    `effective:${picked.type}:${String(picked.gitlabId)}#${String(version)}`,
    (signal) => effectivePolicies(picked.type, picked.gitlabId, signal),
  );
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Working out what applies…
      </p>
    );
  }
  return <ResolutionView resolution={loaded.data} />;
}

/** One resolution: the inheritance chain, what is in effect and where from, and what isn't. */
export function ResolutionView({ resolution }: { resolution: Resolution }) {
  const here = resolution.target;
  // The API lists the path nearest first; people read it top down, from the instance.
  const chain = [...resolution.path].reverse().map(where);
  const notInEffect = resolution.considered.filter((item) => item.outcome !== "effective");
  return (
    <div className="space-y-3 text-sm">
      <p aria-label="Inheritance chain">
        {chain.map((step, index) => (
          <span key={`${step}-${String(index)}`}>
            {index > 0 && <span aria-hidden="true">{" ▸ "}</span>}
            <span className={index === chain.length - 1 ? "font-semibold" : ""}>{step}</span>
          </span>
        ))}
      </p>
      {resolution.ancestry === "incomplete" && (
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-amber-900">
          {"Partly unknown: Baluarte can't confirm every group above this one ("}
          {resolution.gaps
            .map(
              (gap) => `group ${String(gap.gitlab_id)} is ${gapReasons[gap.reason] ?? gap.reason}`,
            )
            .join("; ")}
          {
            "), so baselines applied above it may be missing. Results that depend on them are unknown, not passing."
          }
        </p>
      )}
      {resolution.policies.length === 0 ? (
        <p className="rounded border border-dashed border-slate-300 p-3 text-slate-600">
          {`Nothing is checked for ${resolution.target.path} yet. Apply a baseline to it, to one of its groups, or to the whole instance.`}
        </p>
      ) : (
        <div>
          <p className="font-medium">In effect</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {resolution.policies.map((policy) => (
              <li key={policy.assignment_id}>
                <Link to={baselineHref(policy.name, policy.version)} className={linkClass}>
                  {`${policy.name} v${String(policy.version)}`}
                </Link>
                {policy.assigned_to.target_type === here.target_type &&
                policy.assigned_to.target_id === here.target_id
                  ? " — set here"
                  : ` — from ${where(policy.assigned_to)}`}
              </li>
            ))}
          </ul>
        </div>
      )}
      {notInEffect.length > 0 && (
        <div>
          <p className="font-medium">Also applied above, but not in effect here</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {notInEffect.map((item) => (
              <li key={item.assignment_id}>
                {`${item.name} v${String(item.version)} from ${where(item.assigned_to)}: ${outcomeLabels[item.outcome]}`}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** What is checked for one group or project, and why: the API's own resolution. */
export function LookupPanel({ version }: { version: number }) {
  const [type, setType] = useState<"group" | "project">("project");
  const [picked, setPicked] = useState<Picked | null>(null);

  return (
    <section
      aria-labelledby="lookup"
      className="space-y-3 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="lookup" className="text-lg font-semibold">
        Check a project or group
      </h2>
      <p className="text-sm text-slate-600">
        Find what is checked for one project or group, and where each baseline comes from.
      </p>
      <fieldset>
        <legend className="text-sm font-medium">Look up a</legend>
        <div className="mt-1 flex gap-4">
          {(["project", "group"] as const).map((option) => (
            <label key={option} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="lookup-type"
                checked={type === option}
                onChange={() => {
                  setType(option);
                  setPicked(null);
                }}
              />
              {targetTypeLabels[option]}
            </label>
          ))}
        </div>
      </fieldset>
      <TargetPicker key={type} type={type} value={picked} onChange={setPicked} />
      {picked && <Result picked={picked} version={version} />}
    </section>
  );
}
