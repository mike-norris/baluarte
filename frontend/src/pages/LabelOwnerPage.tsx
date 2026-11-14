import { Link, useParams } from "react-router";

import { getOwnerLabels, type LabelOwner, type OwnerLabels } from "../api/labels";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { ErrorMessage } from "../components/ErrorMessage";
import { DriftTable } from "../labels/DriftTable";
import { inventoryHref, LABELS, ownerHref, ownerText } from "../labels/format";
import { Swatch } from "../labels/Swatch";
import { NotFoundPage } from "./NotFoundPage";
import { formatTime } from "../time";
import { PageHeader } from "../ui/PageHeader";

const cell = "align-top";

function sameOwner(a: LabelOwner, b: LabelOwner): boolean {
  return a.type === b.type && a.gitlab_id === b.gitlab_id;
}

function Labels({ data }: { data: OwnerLabels }) {
  if (data.status === "unknown") {
    return (
      <p className="alert alert-warning text-body">
        {`Unknown: the labels here could not be read${data.reason ? ` (${data.reason})` : ""}. They are not counted as missing or as matching the taxonomy.`}
      </p>
    );
  }
  if (data.labels.length === 0) {
    return <p className="text-secondary">No labels are defined here or on any group above.</p>;
  }
  return (
    <div className="card table-responsive">
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">Labels available here</caption>
        <thead>
          <tr>
            {["Label", "Colour", "Description", "Defined on"].map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
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
                  <Link to={ownerHref(label.defined_at)}>{ownerText(label.defined_at)}</Link>
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
      <p role="status" className="text-secondary">
        Loading labels…
      </p>
    );
  }
  const data = loaded.data;
  return (
    <section aria-labelledby="page-title">
      <div>
        <PageHeader pretitle={<Link to={LABELS}>Labels</Link>} title={ownerText(data.owner)} />
        <p className="text-secondary">
          {data.observation
            ? `Labels read ${formatTime(data.observation.collected_at)} (observation ${String(data.observation.id)}).`
            : "No label read has been recorded here."}{" "}
          <Link to={inventoryHref(data.owner)}>Open in the inventory</Link>
        </p>
        {data.observation?.payload_digest && (
          <p className="text-break font-monospace small text-secondary">
            {`SHA-256 ${data.observation.payload_digest}`}
          </p>
        )}
      </div>
      <AsOfResult answer={data} />
      <section aria-labelledby="labels-title">
        <h2 id="labels-title" className="card-title">
          Labels available here
        </h2>
        <Labels data={data} />
      </section>
      <section aria-labelledby="owner-drift-title">
        <h2 id="owner-drift-title" className="card-title">
          Drift from the taxonomy
        </h2>
        {data.drift.length === 0 ? (
          <p className="text-secondary">
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
