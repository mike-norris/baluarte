import { Link, useSearchParams } from "react-router";

import {
  controlPath,
  controlStatuses,
  frameworkIds,
  listControls,
  listMappingChanges,
  type Control,
  type ControlStatus,
  type FrameworkId,
} from "../api/controls";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { fieldClass, linkClass } from "../components/styles";
import { frameworkLabels, ruleTypeLabel, statusLabels } from "../controls/format";
import { StatusBadge } from "../controls/StatusBadge";
import { CHANGES_VIEW } from "../policies/format";

const cell = "px-3 py-2 align-top";

function readFramework(value: string | null): FrameworkId {
  return frameworkIds.find((id) => id === value) ?? "soc2";
}

function readStatus(value: string | null): ControlStatus | "" {
  return controlStatuses.find((status) => status === value) ?? "";
}

function Mapped({ control }: { control: Control }) {
  if (control.mapped.length === 0) return <span className="text-slate-600">None</span>;
  return (
    <ul className="space-y-1">
      {control.mapped.map((rule) => (
        <li key={rule.rule_type}>
          {ruleTypeLabel(rule.rule_type)}{" "}
          <span className="rounded bg-slate-100 px-1 text-xs text-slate-700">
            {rule.source === "default" ? "Built in" : "Organization"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Results({ control }: { control: Control }) {
  const { counts } = control;
  if (counts.pass + counts.fail + counts.unknown + counts.excepted === 0) {
    return <span className="text-slate-600">No results</span>;
  }
  return (
    <span className="whitespace-nowrap">
      {`${String(counts.pass)} pass, ${String(counts.fail)} fail, ${String(counts.unknown)} unknown`}
      {counts.excepted > 0 && `, ${String(counts.excepted)} excepted`}
      <span className="block text-xs text-slate-600">{`${String(counts.projects)} projects`}</span>
    </span>
  );
}

/** Mapping changes waiting for someone else's approval; nothing is shown when it can't be read. */
function PendingNotice() {
  const pending = useLoad("control-mapping:proposed", (signal) =>
    listMappingChanges("proposed", signal),
  );
  const waiting = pending.data?.items.length ?? 0;
  if (waiting === 0) return null;
  return (
    <p role="status" className="rounded border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
      {`${String(waiting)} mapping ${waiting === 1 ? "change is" : "changes are"} waiting for review. `}
      <Link to={CHANGES_VIEW} className={linkClass}>
        Review changes
      </Link>
    </p>
  );
}

/** Framework controls, the baseline rules that count toward each, and their posture. */
export function ControlsPage() {
  const [params, setParams] = useSearchParams();
  const framework = readFramework(params.get("framework"));
  const status = readStatus(params.get("status"));
  const loaded = useLoad(`controls:${framework}`, (signal) => listControls(framework, signal));

  function choose(next: { framework?: FrameworkId; status?: ControlStatus | "" }) {
    const query = new URLSearchParams();
    const nextFramework = next.framework ?? framework;
    const nextStatus = next.status ?? status;
    if (nextFramework !== "soc2") query.set("framework", nextFramework);
    if (nextStatus) query.set("status", nextStatus);
    setParams(query);
  }

  let body;
  if (loaded.error) body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  else if (!loaded.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading controls…
      </p>
    );
  } else {
    const all = loaded.data.controls;
    const shown = status ? all.filter((control) => control.status === status) : all;
    const tally = new Map<ControlStatus, number>();
    for (const control of all) tally.set(control.status, (tally.get(control.status) ?? 0) + 1);
    body = (
      <div className="space-y-3">
        <p className="text-sm text-slate-700">
          {controlStatuses
            .filter((item) => tally.has(item))
            .map((item) => `${String(tally.get(item))} ${statusLabels[item].toLowerCase()}`)
            .join(", ")}
          {` (mapping version ${loaded.data.mapping_version})`}
        </p>
        <label className="block max-w-xs text-sm font-medium">
          Status
          <select
            value={status}
            onChange={(event) => {
              choose({ status: readStatus(event.target.value) });
            }}
            className={fieldClass}
          >
            <option value="">Any</option>
            {controlStatuses.map((item) => (
              <option key={item} value={item}>
                {statusLabels[item]}
              </option>
            ))}
          </select>
        </label>
        {shown.length === 0 ? (
          <p className="text-sm text-slate-600">No control has this status.</p>
        ) : (
          <div className="overflow-x-auto rounded border border-slate-200 bg-white">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">{`${frameworkLabels[framework]} controls`}</caption>
              <thead className="border-b border-slate-200 bg-slate-50">
                <tr>
                  {["Control", "Status", "Mapped rules", "Results"].map((header) => (
                    <th key={header} scope="col" className={`${cell} font-medium`}>
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {shown.map((control) => (
                  <tr key={control.id}>
                    <th scope="row" className={`${cell} font-normal`}>
                      <Link
                        to={controlPath(control.framework, control.id)}
                        className={`${linkClass} font-medium`}
                      >
                        {control.id}
                      </Link>
                      <span className="block">{control.title}</span>
                      <span className="block text-xs text-slate-600">{control.area}</span>
                    </th>
                    <td className={cell}>
                      <StatusBadge status={control.status} />
                    </td>
                    <td className={cell}>
                      <Mapped control={control} />
                    </td>
                    <td className={cell}>
                      <Results control={control} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Controls
        </h1>
        <p className="mt-2 max-w-3xl text-slate-600">
          Which baseline rules count toward each SOC 2, ISO 27001 and NIST 800-53 control, and
          how those rules are doing. The built-in mapping is guidance; your organization can
          adjust it with a justification. It is not a certification: your auditor decides
          whether a control is met.
        </p>
      </div>
      <PendingNotice />
      <nav aria-label="Frameworks">
        <ul className="flex flex-wrap gap-1 border-b border-slate-200">
          {frameworkIds.map((id) => (
            <li key={id}>
              <button
                type="button"
                aria-current={id === framework ? "page" : undefined}
                onClick={() => {
                  choose({ framework: id, status: "" });
                }}
                className={`-mb-px block border-b-2 px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${
                  id === framework
                    ? "border-slate-900 text-slate-900"
                    : "border-transparent text-slate-600 hover:text-slate-900"
                }`}
              >
                {frameworkLabels[id]}
              </button>
            </li>
          ))}
        </ul>
      </nav>
      {body}
    </section>
  );
}
