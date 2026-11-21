import { useState, type FormEvent } from "react";

import {
  createPeriod,
  endPeriod,
  gapLabels,
  getCoverage,
  listMappings,
  listPeriods,
  type AuditPeriod,
  type CoverageRow,
} from "../api/register";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { frameworkLabel } from "../controls/format";
import { asApiError, Problems } from "../settings/parts";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { SectionTabs } from "../ui/SectionTabs";

import { REGISTER_TABS } from "./registerTabs";
import { ExportButton, useCanExport } from "./RegisterExport";
import { MappingCard, PeriodSelect } from "./RegisterMappings";

function useUser() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  return {
    csrf: user?.csrf_token ?? "",
    canWrite: user?.permissions.includes("register:write") ?? false,
  };
}

function ControlMappings({
  row,
  periodId,
  onChanged,
}: {
  row: CoverageRow;
  periodId: number | null;
  onChanged: () => void;
}) {
  const mappings = useLoad(
    `register-control:${row.framework}:${row.control_id}:${String(periodId)}`,
    (signal) => listMappings(row.framework, row.control_id, periodId, signal),
  );
  if (mappings.loading) return <Loading label="Loading the mapped issues…" />;
  if (mappings.error) return <ErrorMessage error={mappings.error} retry={mappings.retry} />;
  const open = (mappings.data?.items ?? []).filter(
    (m) => m.state === "active" || m.state === "proposed",
  );
  return (
    <div className="stack-2">
      {open.map((mapping) => (
        <MappingCard
          key={mapping.id}
          mapping={mapping}
          showIssue
          onChanged={() => {
            mappings.retry();
            onChanged();
          }}
        />
      ))}
    </div>
  );
}

function Coverage({ periodId }: { periodId: number | null }) {
  const [version, setVersion] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const canExport = useCanExport();
  const coverage = useLoad(`register-coverage:${String(periodId)}:${String(version)}`, (signal) =>
    getCoverage(periodId, signal),
  );
  if (coverage.loading) return <Loading label="Loading the coverage…" />;
  if (coverage.error) return <ErrorMessage error={coverage.error} retry={coverage.retry} />;
  const rows = coverage.data?.rows ?? [];
  if (rows.length === 0) {
    return (
      <EmptyState title="No issues mapped to controls yet">
        Open an auditable issue and map it to a control, citing its captured evidence.
      </EmptyState>
    );
  }
  const controls = rows
    .filter((row) => chosen.includes(`${row.framework}:${row.control_id}`))
    .map((row) => ({ framework: row.framework, control_id: row.control_id }));
  return (
    <section aria-label="Coverage" className="card">
      {canExport && (
        <div className="card-body border-bottom">
          <ExportButton
            label={
              controls.length === 0
                ? "Choose controls to export"
                : `Export ${String(controls.length)} ${controls.length === 1 ? "control" : "controls"}${periodId !== null ? " for the period" : ""}`
            }
            request={controls.length === 0 ? null : { controls, period_id: periodId }}
          />
        </div>
      )}
      <div className="table-responsive">
        <table className="table table-vcenter card-table">
          <thead>
            <tr>
              {canExport && (
                <th className="w-1">
                  <span className="visually-hidden">Export</span>
                </th>
              )}
              <th>Control</th>
              <th>Issues in force</th>
              <th>Awaiting review</th>
              {periodId !== null && <th>With evidence in the period</th>}
              <th>Gaps</th>
              <th>
                <span className="visually-hidden">Details</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const key = `${row.framework}:${row.control_id}`;
              const expanded = open === key;
              return [
                <tr key={key}>
                  {canExport && (
                    <td>
                      <input
                        type="checkbox"
                        className="form-check-input"
                        aria-label={`Export ${frameworkLabel(row.framework)} ${row.control_id}`}
                        checked={chosen.includes(key)}
                        onChange={(event) => {
                          const { checked } = event.target;
                          setChosen((current) =>
                            checked ? [...current, key] : current.filter((k) => k !== key),
                          );
                        }}
                      />
                    </td>
                  )}
                  <td>
                    <div>
                      {frameworkLabel(row.framework)} {row.control_id}
                    </div>
                    <div className="small text-secondary">{row.control_title}</div>
                  </td>
                  <td>{row.issues}</td>
                  <td>{row.proposed}</td>
                  {periodId !== null && (
                    <td>{row.in_period === 0 ? <Badge tone="warning">0</Badge> : row.in_period}</td>
                  )}
                  <td>
                    {Object.keys(row.gaps).length === 0
                      ? "None"
                      : Object.entries(row.gaps)
                          .map(([code, count]) => `${gapLabels[code] ?? code}: ${String(count)}`)
                          .join(", ")}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn btn-sm"
                      aria-expanded={expanded}
                      onClick={() => {
                        setOpen(expanded ? null : key);
                      }}
                    >
                      {expanded ? "Hide issues" : "Show issues"}
                    </button>
                  </td>
                </tr>,
                expanded && (
                  <tr key={`${key}-issues`}>
                    <td colSpan={(periodId !== null ? 6 : 5) + (canExport ? 1 : 0)}>
                      <ControlMappings
                        row={row}
                        periodId={periodId}
                        onChanged={() => {
                          setVersion((current) => current + 1);
                        }}
                      />
                    </td>
                  </tr>
                ),
              ];
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Periods({ periods, onChanged }: { periods: AuditPeriod[]; onChanged: () => void }) {
  const { csrf, canWrite } = useUser();
  const [name, setName] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [endsOn, setEndsOn] = useState("");
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setProblems([]);
    try {
      await action();
      onChanged();
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      await createPeriod({ name: name.trim(), starts_on: startsOn, ends_on: endsOn }, csrf);
      setName("");
      setStartsOn("");
      setEndsOn("");
    });
  }

  return (
    <section aria-label="Audit periods" className="card card-body stack-2">
      <h2 className="fw-semibold mb-0">Audit periods</h2>
      <p className="small text-secondary mb-0">
        Evidence dated inside a period is kept apart from later remediation. Periods may overlap,
        one per framework for example.
      </p>
      {periods.length > 0 && (
        <ul className="mb-0">
          {periods.map((period) => (
            <li key={period.id}>
              {period.name}: {period.starts_on} to {period.ends_on}
              {period.ended_at ? (
                <span className="text-secondary"> (ended {formatTime(period.ended_at)})</span>
              ) : (
                canWrite && (
                  <button
                    type="button"
                    className="btn btn-sm ms-2"
                    disabled={busy}
                    onClick={() => void run(() => endPeriod(period.id, csrf))}
                  >
                    End
                  </button>
                )
              )}
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <form aria-label="New audit period" className="row g-2 align-items-end" onSubmit={submit}>
          <label className="form-label col-sm-5">
            Name
            <input
              className="form-control"
              required
              maxLength={120}
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
          </label>
          <label className="form-label col-sm-3">
            Starts on
            <input
              type="date"
              className="form-control"
              required
              value={startsOn}
              onChange={(event) => {
                setStartsOn(event.target.value);
              }}
            />
          </label>
          <label className="form-label col-sm-3">
            Ends on
            <input
              type="date"
              className="form-control"
              required
              value={endsOn}
              onChange={(event) => {
                setEndsOn(event.target.value);
              }}
            />
          </label>
          <div className="col-sm-1">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              Add
            </button>
          </div>
        </form>
      )}
      <Problems messages={problems} />
    </section>
  );
}

export function RegisterControlsPage() {
  const [periodId, setPeriodId] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const periods = useLoad(`register-periods:${String(version)}`, (signal) => listPeriods(signal));
  const items = periods.data?.items ?? [];

  return (
    <div className="stack-3">
      <PageHeader
        pretitle="Evidence"
        title="Issue register"
        subtitle="Controls with auditable issues mapped to them, what each cites, and the gaps."
      />
      <SectionTabs label="Issue register" tabs={REGISTER_TABS} />
      {periods.error && <ErrorMessage error={periods.error} retry={periods.retry} />}
      {items.length > 0 && (
        <div style={{ maxWidth: "32rem" }}>
          <PeriodSelect periods={items} value={periodId} onChange={setPeriodId} />
        </div>
      )}
      <Coverage periodId={periodId} />
      <Periods
        periods={items}
        onChanged={() => {
          setVersion((current) => current + 1);
        }}
      />
    </div>
  );
}
