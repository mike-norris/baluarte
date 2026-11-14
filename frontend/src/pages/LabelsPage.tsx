import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { ISSUE_PLANS } from "../api/issuePlans";
import {
  CAN_PLAN,
  CAN_PROPOSE,
  driftKinds,
  getTaxonomy,
  listDrift,
  type DriftFilters,
  type DriftItem,
  type DriftPage,
  type DriftTotals,
  type TaxonomyVersion,
} from "../api/labels";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { PastReadOnly } from "../asof/PastReadOnly";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  driftKindLabels,
  driftKindMeaning,
  hasDriftFilters,
  LABEL_FINDINGS,
  LABEL_PLANS,
  LABELS,
  readDriftFilters,
  TAXONOMY_EDITOR,
  taxonomyStateLabels,
  writeDriftFilters,
} from "../labels/format";
import { DriftTable } from "../labels/DriftTable";
import { Swatch } from "../labels/Swatch";
import { CHANGES_VIEW, personText } from "../policies/format";
import { hasPermission } from "../remediation/format";
import { formatTime } from "../time";
import { PageHeader } from "../ui/PageHeader";
import { useDeviceClass } from "../ui/device";

const cell = "align-top";

function Taxonomy({ version }: { version: TaxonomyVersion }) {
  const spec = version.spec;
  const decided = version.self_approved
    ? "Not reviewed: review was off, so it applied at once"
    : `Approved by ${personText(version.decided_by)}`;
  return (
    <div>
      <dl className="grid gap-x-6 row-gap-1 sm:grid-cols-[max-content_1fr]">
        <dt className="fw-medium">Taxonomy</dt>
        <dd>{`${version.name} v${String(version.version)} (${taxonomyStateLabels[version.state]})`}</dd>
        <dt className="fw-medium">In force since</dt>
        <dd>{formatTime(version.in_force_from)}</dd>
        <dt className="fw-medium">Proposed by</dt>
        <dd>{`${personText(version.proposed_by)}; ${decided}`}</dd>
        {spec && (
          <>
            <dt className="fw-medium">Groups</dt>
            <dd>{spec.scope.groups.join(", ")}</dd>
            <dt className="fw-medium">Defined at</dt>
            <dd>
              {spec.placement === "top_level_group" ? "Each top-level group" : spec.placement}
            </dd>
            <dt className="fw-medium">Other labels</dt>
            <dd>
              {spec.strict
                ? "Strict: labels not in the taxonomy count as drift"
                : "Allowed: only the taxonomy's labels are checked"}
            </dd>
          </>
        )}
        <dt className="fw-medium">Digest</dt>
        <dd className="text-break font-monospace small">{version.digest}</dd>
      </dl>
      {spec && (
        <div className="card table-responsive">
          <table className="table table-vcenter card-table mb-0">
            <caption className="visually-hidden">Labels in the taxonomy</caption>
            <thead>
              <tr>
                {["Label", "Colour", "Description", "Renamed from"].map((header) => (
                  <th key={header} scope="col" className={`${cell} font-medium`}>
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {spec.labels.map((label) => (
                <tr key={label.name}>
                  <th scope="row" className={`${cell} font-medium`}>
                    {label.name}
                  </th>
                  <td className={cell}>
                    <Swatch color={label.color} />
                  </td>
                  <td className={cell}>{label.description ?? ""}</td>
                  <td className={cell}>
                    {label.aliases.length > 0 ? label.aliases.join(", ") : "None"}
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

function TaxonomySection() {
  const asOf = useAsOf();
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const device = useDeviceClass();
  // Phones can approve or reject a proposed taxonomy, but not propose one.
  const canPropose = user !== null && hasPermission(user, CAN_PROPOSE) && device !== "phone";
  const taxonomy = useLoad(`labels:taxonomy@${asOf ?? "now"}`, (signal) =>
    getTaxonomy(asOf, signal),
  );

  let body;
  if (taxonomy.error) body = <ErrorMessage error={taxonomy.error} retry={taxonomy.retry} />;
  else if (!taxonomy.data) {
    body = (
      <p role="status" className="text-secondary">
        Loading the label taxonomy…
      </p>
    );
  } else {
    const { current, versions } = taxonomy.data;
    const waiting = versions.filter((version) => version.state === "proposed").length;
    body = (
      <>
        {current ? (
          <Taxonomy version={current} />
        ) : (
          <p className="text-secondary border border-dashed rounded p-3">
            {asOf
              ? "No label taxonomy was in force then."
              : "No label taxonomy is in force. Define one to see where groups and projects drift from it."}
          </p>
        )}
        {waiting > 0 && !asOf && (
          <p>
            {`${String(waiting)} proposed ${waiting === 1 ? "taxonomy is" : "taxonomies are"} waiting for review. `}
            <Link to={CHANGES_VIEW}>Open the review queue</Link>
          </p>
        )}
        {canPropose &&
          (asOf ? (
            <PastReadOnly />
          ) : (
            <p>
              <Link to={TAXONOMY_EDITOR} className="btn btn-primary">
                {current ? "Propose a change to the taxonomy" : "Define the taxonomy"}
              </Link>
            </p>
          ))}
      </>
    );
  }
  return (
    <section aria-labelledby="taxonomy-title">
      <h2 id="taxonomy-title" className="card-title">
        Taxonomy in force
      </h2>
      {body}
    </section>
  );
}

function kindHref(params: URLSearchParams, kind: string): string {
  const next = new URLSearchParams(params);
  next.set("kind", kind);
  return `${LABELS}?${next.toString()}`;
}

function groupFilterHref(params: URLSearchParams, group: string): string {
  const next = new URLSearchParams(params);
  next.set("group", group);
  return `${LABELS}?${next.toString()}`;
}

function Summary({ totals }: { totals: DriftTotals }) {
  const [params] = useSearchParams();
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <section aria-labelledby="by-kind-title">
        <h3 id="by-kind-title" className="fw-medium">
          {`Drift by kind (${String(totals.total)} in all)`}
        </h3>
        <ul className="list-group">
          {driftKinds.map((kind) => (
            <li
              key={kind}
              className="d-flex align-items-baseline justify-content-between gap-3 px-3 py-2"
            >
              <span>
                <Link to={kindHref(params, kind)}>{driftKindLabels[kind]}</Link>
                <span className="d-block small text-secondary">{driftKindMeaning[kind]}</span>
              </span>
              <span className="fw-medium tabular-nums">{totals.by_kind[kind] ?? 0}</span>
            </li>
          ))}
        </ul>
      </section>
      <section aria-labelledby="by-group-title">
        <h3 id="by-group-title" className="fw-medium">
          Drift by top-level group
        </h3>
        {totals.by_group.length === 0 ? (
          <p className="text-secondary">No drift in any group.</p>
        ) : (
          <div className="card table-responsive">
            <table className="table table-vcenter card-table mb-0">
              <caption className="visually-hidden">Drift by top-level group</caption>
              <thead>
                <tr>
                  <th scope="col" className={`${cell} font-medium`}>
                    Group
                  </th>
                  <th scope="col" className={`${cell} font-medium`}>
                    Drift
                  </th>
                  <th scope="col" className={`${cell} font-medium`}>
                    Unknown
                  </th>
                </tr>
              </thead>
              <tbody>
                {totals.by_group.map((row) => (
                  <tr key={row.group}>
                    <th scope="row" className={`${cell} font-medium`}>
                      <Link to={groupFilterHref(params, row.group)}>{row.group}</Link>
                    </th>
                    <td className={`${cell} tabular-nums`}>{row.total}</td>
                    <td className={`${cell} tabular-nums`}>{row.by_kind.unknown ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

function Filters({ filters }: { filters: DriftFilters }) {
  const [params, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = readDriftFilters(
      new URLSearchParams({
        group: text(form, "group"),
        kind: text(form, "kind"),
        label: text(form, "label"),
      }),
    );
    setParams(writeDriftFilters(next, params));
  }

  function clear() {
    setParams(writeDriftFilters({}, params));
  }

  return (
    <form
      key={writeDriftFilters(filters).toString()}
      onSubmit={apply}
      aria-label="Filter label drift"
      className="d-flex flex-wrap align-items-end gap-3 rounded border p-3"
    >
      <label className="fw-medium">
        Top-level group
        <input name="group" defaultValue={filters.group ?? ""} className="form-control mt-1" />
      </label>
      <label className="fw-medium">
        Kind
        <select name="kind" defaultValue={filters.kind ?? ""} className="form-control mt-1">
          <option value="">Any</option>
          {driftKinds.map((kind) => (
            <option key={kind} value={kind}>
              {driftKindLabels[kind]}
            </option>
          ))}
        </select>
      </label>
      <label className="fw-medium">
        Label
        <input name="label" defaultValue={filters.label ?? ""} className="form-control mt-1" />
      </label>
      <button type="submit" className="btn btn-primary">
        Apply filters
      </button>
      {hasDriftFilters(filters) && (
        <button type="button" onClick={clear} className="btn">
          Clear
        </button>
      )}
    </form>
  );
}

function Drift({ filters }: { filters: DriftFilters }) {
  const asOf = useAsOf();
  const key = `${writeDriftFilters(filters).toString()}@${asOf ?? "now"}`;
  const first = useLoad(`labels:drift?${key}`, (signal) => listDrift(filters, asOf, null, signal));
  const paged = usePaged<DriftItem, DriftPage, string>(
    key,
    first,
    (page) => page.next_after,
    (after) => listDrift(filters, asOf, after),
  );

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (!first.data) {
    return (
      <p role="status" className="text-secondary">
        Loading label drift…
      </p>
    );
  }
  const page = first.data;
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <div>
      <AsOfResult answer={page} />
      <p className="text-secondary">
        {page.taxonomy
          ? `Measured against ${page.taxonomy.name} v${String(page.taxonomy.version)}, from labels read ${formatTime(page.collected_at)}.`
          : "No taxonomy was in force, so there is nothing to measure drift against."}
      </p>
      <Summary totals={page.totals} />
      <Filters filters={filters} />
      {paged.items.length === 0 ? (
        <p className="text-secondary border border-dashed rounded p-3">
          {hasDriftFilters(filters)
            ? "No drift matches these filters."
            : "No drift: every group and project in scope matches the taxonomy."}
        </p>
      ) : (
        <>
          <DriftTable items={paged.items} caption="Label drift by group or project" />
          <p className="text-secondary" aria-live="polite">
            {`Showing ${String(paged.items.length)} of ${String(page.totals.total)} drift items${more}.`}
          </p>
        </>
      )}
      {paged.moreError && (
        <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
      )}
      {paged.next !== null && (
        <button
          type="button"
          onClick={() => void paged.loadMore()}
          disabled={paged.pending}
          className="btn"
        >
          {paged.pending ? "Loading…" : "Load more drift"}
        </button>
      )}
    </div>
  );
}

export function LabelsPage() {
  const [params] = useSearchParams();
  const filters = readDriftFilters(params);
  const { session } = useAuth();
  const canPlan = session.status === "signed_in" && hasPermission(session.user, CAN_PLAN);
  return (
    <section aria-labelledby="page-title">
      <div>
        <PageHeader
          pretitle="Govern"
          title="Labels"
          subtitle="One set of labels for every group: the taxonomy in force, and where groups and projects differ from it. Labels that could not be read are unknown, never counted as missing or matching. Fixes are made through reviewed plans, which never delete a label."
        />
        <p className="d-flex flex-wrap gap-3">
          <Link to={LABEL_FINDINGS}>Label consistency findings</Link>
          {canPlan && <Link to={LABEL_PLANS}>Label plans</Link>}
          {canPlan && <Link to={ISSUE_PLANS}>Closed issue time</Link>}
        </p>
      </div>
      <TaxonomySection />
      <section aria-labelledby="drift-title" className="card card-body mb-3">
        <h2 id="drift-title" className="card-title">
          Drift
        </h2>
        <Drift filters={filters} />
      </section>
    </section>
  );
}
