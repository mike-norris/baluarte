import { Link } from "react-router";

import type { DriftItem } from "../api/labels";
import { linkClass } from "../components/styles";
import { formatTime } from "../time";
import { driftKindLabels, driftKindMeaning, ownerHref, ownerText, valuesText } from "./format";
import { Swatch } from "./Swatch";

const cell = "px-3 py-2 align-top";

function Values({ lines }: { lines: string[] }) {
  if (lines.length === 0) return <span className="text-slate-600">None</span>;
  return (
    <ul className="space-y-0.5">
      {lines.map((line) => (
        <li key={line} className="break-words">
          {line}
        </li>
      ))}
    </ul>
  );
}

function ObservedCell({ item }: { item: DriftItem }) {
  if (item.kind === "unknown") {
    return (
      <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-900">
        {`Unknown${item.reason ? `: ${item.reason}` : ""}`}
      </span>
    );
  }
  const observed = item.observed;
  return (
    <div className="space-y-1">
      {observed?.color && <Swatch color={observed.color} />}
      <Values lines={valuesText(observed ? { ...observed, color: null } : null)} />
      {observed?.defined_at && (
        <p className="text-xs text-slate-600">{`Defined on ${ownerText(observed.defined_at)}`}</p>
      )}
    </div>
  );
}

function ExpectedCell({ item }: { item: DriftItem }) {
  const expected = item.expected;
  return (
    <div className="space-y-1">
      {expected?.color && <Swatch color={expected.color} />}
      <Values lines={valuesText(expected ? { ...expected, color: null } : null)} />
      {expected?.placement && (
        <p className="text-xs text-slate-600">{`Belongs on ${ownerText(expected.placement)}`}</p>
      )}
    </div>
  );
}

function DriftRow({ item }: { item: DriftItem }) {
  const evidence = item.observations;
  return (
    <tr>
      <th scope="row" className={`${cell} font-medium`}>
        <Link to={ownerHref(item.owner)} className={linkClass}>
          {item.owner.path}
        </Link>
        <span className="block text-xs font-normal text-slate-600">
          {item.owner.type === "group" ? "Group" : "Project"}
        </span>
      </th>
      <td className={cell}>
        <span title={driftKindMeaning[item.kind]}>{driftKindLabels[item.kind]}</span>
      </td>
      <td className={cell}>{item.label ?? "All labels"}</td>
      <td className={cell}>
        <ObservedCell item={item} />
      </td>
      <td className={cell}>
        <ExpectedCell item={item} />
      </td>
      <td className={cell}>
        {evidence.length === 0 ? (
          <span className="text-slate-600">No observation</span>
        ) : (
          <ul className="space-y-0.5 text-xs">
            {evidence.map((observation) => (
              <li key={observation.id}>
                {`Observation ${String(observation.id)}, ${formatTime(observation.collected_at)}`}
              </li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  );
}

const headers = ["Group or project", "Kind", "Label", "Observed", "Expected", "Evidence"];

/** Drift items with what was observed, what the taxonomy expects and the evidence. */
export function DriftTable({ items, caption }: { items: DriftItem[]; caption: string }) {
  return (
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            {headers.map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {items.map((item) => (
            <DriftRow key={item.id} item={item} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
