import { Link } from "react-router";

import type { DriftItem } from "../api/labels";
import { formatTime } from "../time";
import { driftKindLabels, driftKindMeaning, ownerHref, ownerText, valuesText } from "./format";
import { Swatch } from "./Swatch";

const cell = "align-top";

function Values({ lines }: { lines: string[] }) {
  if (lines.length === 0) return <span className="text-secondary">None</span>;
  return (
    <ul>
      {lines.map((line) => (
        <li key={line} className="text-break">
          {line}
        </li>
      ))}
    </ul>
  );
}

function ObservedCell({ item }: { item: DriftItem }) {
  if (item.kind === "unknown") {
    return (
      <span className="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle">
        {`Unknown${item.reason ? `: ${item.reason}` : ""}`}
      </span>
    );
  }
  const observed = item.observed;
  return (
    <div>
      {observed?.color && <Swatch color={observed.color} />}
      <Values lines={valuesText(observed ? { ...observed, color: null } : null)} />
      {observed?.defined_at && (
        <p className="small text-secondary">{`Defined on ${ownerText(observed.defined_at)}`}</p>
      )}
    </div>
  );
}

function ExpectedCell({ item }: { item: DriftItem }) {
  const expected = item.expected;
  return (
    <div>
      {expected?.color && <Swatch color={expected.color} />}
      <Values lines={valuesText(expected ? { ...expected, color: null } : null)} />
      {expected?.placement && (
        <p className="small text-secondary">{`Belongs on ${ownerText(expected.placement)}`}</p>
      )}
    </div>
  );
}

function DriftRow({ item }: { item: DriftItem }) {
  const evidence = item.observations;
  return (
    <tr>
      <th scope="row" className={`${cell} font-medium`}>
        <Link to={ownerHref(item.owner)}>{item.owner.path}</Link>
        <span className="d-block small fw-normal text-secondary">
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
          <span className="text-secondary">No observation</span>
        ) : (
          <ul className="small">
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
    <div className="card table-responsive">
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {headers.map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <DriftRow key={item.id} item={item} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
