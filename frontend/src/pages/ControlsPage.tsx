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
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { ErrorMessage } from "../components/ErrorMessage";
import { frameworkLabels, ruleTypeLabel, statusLabels } from "../controls/format";
import { StatusBadge } from "../controls/StatusBadge";
import { CHANGES_VIEW } from "../policies/format";
import { Badge } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

function readFramework(value: string | null): FrameworkId {
  return frameworkIds.find((id) => id === value) ?? "soc2";
}

function readStatus(value: string | null): ControlStatus | "" {
  return controlStatuses.find((status) => status === value) ?? "";
}

function Mapped({ control }: { control: Control }) {
  if (control.mapped.length === 0) return <span className="text-secondary">None</span>;
  return (
    <ul className="list-unstyled mb-0">
      {control.mapped.map((rule) => (
        <li key={rule.rule_type}>
          {ruleTypeLabel(rule.rule_type)}{" "}
          <Badge tone="secondary">{rule.source === "default" ? "Built in" : "Organization"}</Badge>
        </li>
      ))}
    </ul>
  );
}

function Results({ control }: { control: Control }) {
  const { counts } = control;
  if (counts.pass + counts.fail + counts.unknown + counts.excepted === 0) {
    return <span className="text-secondary">No results</span>;
  }
  return (
    <span className="text-nowrap tabular-nums">
      {`${String(counts.pass)} pass, ${String(counts.fail)} fail, ${String(counts.unknown)} unknown`}
      {counts.excepted > 0 && `, ${String(counts.excepted)} excepted`}
      <span className="d-block small text-secondary">{`${String(counts.projects)} projects`}</span>
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
    <p role="status" className="alert alert-info text-body">
      {`${String(waiting)} mapping ${waiting === 1 ? "change is" : "changes are"} waiting for review. `}
      <Link to={CHANGES_VIEW}>Review changes</Link>
    </p>
  );
}

/** Framework controls, the baseline rules that count toward each, and their posture. */
export function ControlsPage() {
  const [params, setParams] = useSearchParams();
  const framework = readFramework(params.get("framework"));
  const status = readStatus(params.get("status"));
  const asOf = useAsOf();
  const loaded = useLoad(`controls:${framework}@${asOf ?? "now"}`, (signal) =>
    listControls(framework, signal, asOf),
  );

  function choose(next: { framework?: FrameworkId; status?: ControlStatus | "" }) {
    const query = new URLSearchParams();
    if (asOf) query.set("as_of", asOf);
    const nextFramework = next.framework ?? framework;
    const nextStatus = next.status ?? status;
    if (nextFramework !== "soc2") query.set("framework", nextFramework);
    if (nextStatus) query.set("status", nextStatus);
    setParams(query);
  }

  const columns: readonly Column<Control>[] = [
    {
      key: "control",
      header: "Control",
      rowHeader: true,
      render: (control) => (
        <>
          <Link to={controlPath(control.framework, control.id)} className="fw-medium">
            {control.id}
          </Link>
          <span className="d-block">{control.title}</span>
          <span className="d-block small text-secondary">{control.area}</span>
        </>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (control) => <StatusBadge status={control.status} />,
    },
    {
      key: "mapped",
      header: "Mapped rules",
      render: (control) => <Mapped control={control} />,
      priority: 3,
    },
    {
      key: "results",
      header: "Results",
      render: (control) => <Results control={control} />,
      priority: 2,
    },
  ];

  let body;
  if (loaded.error) {
    body = (
      <div className="card-body">
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      </div>
    );
  } else if (!loaded.data) {
    body = (
      <div className="card-body">
        <Loading label="Loading controls…" />
      </div>
    );
  } else {
    const all = loaded.data.controls;
    const shown = status ? all.filter((control) => control.status === status) : all;
    const tally = new Map<ControlStatus, number>();
    for (const control of all) tally.set(control.status, (tally.get(control.status) ?? 0) + 1);
    body = (
      <>
        <div className="card-body border-bottom">
          <AsOfResult answer={loaded.data} />
          <div className="d-flex flex-wrap align-items-end gap-3">
            <label className="form-label mb-0">
              Status
              <select
                value={status}
                onChange={(event) => {
                  choose({ status: readStatus(event.target.value) });
                }}
                className="form-select mt-1"
              >
                <option value="">Any</option>
                {controlStatuses.map((item) => (
                  <option key={item} value={item}>
                    {statusLabels[item]}
                  </option>
                ))}
              </select>
            </label>
            <p className="text-secondary mb-0">
              {controlStatuses
                .filter((item) => tally.has(item))
                .map((item) => `${String(tally.get(item))} ${statusLabels[item].toLowerCase()}`)
                .join(", ")}
              {` (mapping version ${loaded.data.mapping_version})`}
            </p>
          </div>
        </div>
        {shown.length === 0 ? (
          <EmptyState title="No control has this status." />
        ) : (
          <ResponsiveTable
            caption={`${frameworkLabels[framework]} controls`}
            columns={columns}
            rows={shown}
            rowKey={(control) => control.id}
          />
        )}
      </>
    );
  }

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Observe"
        title="Controls"
        subtitle="Which baseline rules count toward each SOC 2, ISO 27001 and NIST 800-53 control, and how those rules are doing. The built-in mapping is guidance; your organization can adjust it with a justification. It is not a certification: your auditor decides whether a control is met."
      />
      <PendingNotice />
      <div className="card">
        <div className="card-header">
          <nav aria-label="Frameworks">
            <ul className="nav nav-tabs card-header-tabs flex-nowrap overflow-auto">
              {frameworkIds.map((id) => (
                <li key={id} className="nav-item">
                  <button
                    type="button"
                    aria-current={id === framework ? "page" : undefined}
                    onClick={() => {
                      choose({ framework: id, status: "" });
                    }}
                    className={`nav-link text-nowrap ${id === framework ? "active" : ""}`}
                  >
                    {frameworkLabels[id]}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
        </div>
        {body}
      </div>
    </section>
  );
}
