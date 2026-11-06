import { Link, useParams } from "react-router";

import { getOwnerLabels, type LabelOwner, type OwnerLabels } from "../api/labels";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { ErrorMessage } from "../components/ErrorMessage";
import { linkClass } from "../components/styles";
import { DriftTable } from "../labels/DriftTable";
import { inventoryHref, LABELS, ownerHref, ownerText } from "../labels/format";
import { Swatch } from "../labels/Swatch";
import { NotFoundPage } from "./NotFoundPage";
import { formatTime } from "../time";

const cell = "px-3 py-2 align-top";

function sameOwner(a: LabelOwner, b: LabelOwner): boolean {
  return a.type === b.type && a.gitlab_id === b.gitlab_id;
}

function Labels({ data }: { data: OwnerLabels }) {
  if (data.status === "unknown") {
    return (
      <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
        {`Unknown: the labels here could not be read${data.reason ? ` (${data.reason})` : ""}. They are not counted as missing or as matching the taxonomy.`}
      </p>
    );
  }
  if (data.labels.length === 0) {
    return <p className="text-sm text-slate-600">No labels are defined here or on any group above.</p>;
  }
  return (
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Labels available here</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            {["Label", "Colour", "Description", "Defined on"].map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {data.labels.map((label) => (
            <tr key={`${String(label.id)}:${label.defined_at.path}`}>
              <th scope="row" className={`${cell} font-medium`}>
                {label.name}
              </th>
              <td className={cell}>
                <Swatch color={label.color} />
              </td>
              <td className={cell}>{label.description ?? ""}</td>
              <td className={cell}>
                {sameOwner(label.defined_at, data.owner) ? (
                  "Here"
                ) : (
                  <Link to={ownerHref(label.defined_at)} className={linkClass}>
                    {ownerText(label.defined_at)}
                  </Link>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OwnerView({ type, gitlabId }: { type: LabelOwner["type"]; gitlabId: number }) {
  const asOf = useAsOf();
  const loaded = useLoad(`labels:owner:${type}:${String(gitlabId)}@${asOf ?? "now"}`, (signal) =>
    getOwnerLabels(type, gitlabId, asOf, signal),
  );
  if (loaded.error) {
    if (loaded.error.status === 404) return <NotFoundPage />;
    return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  }
  if (!loaded.data) {
    return (
      <p role="status" className="text-slate-600">
        Loading labels…
      </p>
    );
  }
  const data = loaded.data;
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <p className="text-sm">
          <Link to={LABELS} className={linkClass}>
            Labels
          </Link>
        </p>
        <h1 id="page-title" className="text-2xl font-semibold">
          {ownerText(data.owner)}
        </h1>
        <p className="mt-2 text-sm text-slate-600">
          {data.observation
            ? `Labels read ${formatTime(data.observation.collected_at)} (observation ${String(data.observation.id)}).`
            : "No label read has been recorded here."}{" "}
          <Link to={inventoryHref(data.owner)} className={linkClass}>
            Open in the inventory
          </Link>
        </p>
        {data.observation?.payload_digest && (
          <p className="break-all font-mono text-xs text-slate-600">
            {`SHA-256 ${data.observation.payload_digest}`}
          </p>
        )}
      </div>
      <AsOfResult answer={data} />
      <section aria-labelledby="labels-title" className="space-y-3">
        <h2 id="labels-title" className="text-lg font-semibold">
          Labels available here
        </h2>
        <Labels data={data} />
      </section>
      <section aria-labelledby="owner-drift-title" className="space-y-3">
        <h2 id="owner-drift-title" className="text-lg font-semibold">
          Drift from the taxonomy
        </h2>
        {data.drift.length === 0 ? (
          <p className="text-sm text-slate-600">
            {data.status === "unknown"
              ? "Not known: the labels could not be read."
              : "None: the labels here match the taxonomy."}
          </p>
        ) : (
          <DriftTable items={data.drift} caption={`Drift on ${data.owner.path}`} />
        )}
      </section>
    </section>
  );
}

export function LabelOwnerPage() {
  const { ownerType = "", gitlabId = "" } = useParams();
  const id = Number(gitlabId);
  if ((ownerType !== "group" && ownerType !== "project") || !Number.isSafeInteger(id) || id <= 0) {
    return <NotFoundPage />;
  }
  return <OwnerView key={`${ownerType}:${String(id)}`} type={ownerType} gitlabId={id} />;
}
