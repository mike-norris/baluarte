import { useState } from "react";
import { Link } from "react-router";

import { frameworkIds } from "../api/controls";
import { gapLabels, getEvidenceGaps, type Gap } from "../api/evidenceGaps";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { frameworkLabel } from "../controls/format";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

const gapTones: Record<Gap, Tone> = {
  missing: "danger",
  stale: "warning",
  inaccessible: "danger",
  incomplete: "info",
};

const GAPS = Object.keys(gapLabels) as Gap[];

/** The last full calendar quarter, as the default period. */
export function lastQuarter(today: Date): { start: string; end: string } {
  const quarter = Math.floor(today.getUTCMonth() / 3);
  const year = quarter === 0 ? today.getUTCFullYear() - 1 : today.getUTCFullYear();
  const first = quarter === 0 ? 9 : (quarter - 1) * 3;
  const start = new Date(Date.UTC(year, first, 1));
  const end = new Date(Date.UTC(year, first + 3, 0));
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
}

/** Missing, stale, inaccessible and incomplete evidence for each control of a framework, for a
 * period. */
export function EvidenceGapsPage() {
  const initial = lastQuarter(new Date());
  const [framework, setFramework] = useState<string>("soc2");
  const [periodStart, setPeriodStart] = useState(initial.start);
  const [periodEnd, setPeriodEnd] = useState(initial.end);
  const [only, setOnly] = useState<Gap | "any" | "">("any");
  const ready = periodStart !== "" && periodEnd !== "";
  const loaded = useLoad(`evidence-gaps:${framework}:${periodStart}:${periodEnd}`, (signal) =>
    ready
      ? getEvidenceGaps({ framework, period_start: periodStart, period_end: periodEnd }, signal)
      : Promise.resolve(null),
  );
  const rows =
    loaded.data?.controls.filter((c) =>
      only === "" ? true : only === "any" ? c.gaps.length > 0 : c.gaps.includes(only),
    ) ?? [];

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Evidence gaps"
        subtitle="Controls with missing, stale, inaccessible or incomplete evidence for a period."
      />
      <form
        aria-label="Gap period"
        className="d-flex flex-wrap gap-2 mb-3"
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <label className="form-label mb-0">
          Framework
          <select
            className="form-select mt-1"
            value={framework}
            onChange={(event) => {
              setFramework(event.target.value);
            }}
          >
            {frameworkIds.map((id) => (
              <option key={id} value={id}>
                {frameworkLabel(id)}
              </option>
            ))}
          </select>
        </label>
        <label className="form-label mb-0">
          Period from
          <input
            className="form-control mt-1"
            type="date"
            value={periodStart}
            onChange={(event) => {
              setPeriodStart(event.target.value);
            }}
          />
        </label>
        <label className="form-label mb-0">
          Period to
          <input
            className="form-control mt-1"
            type="date"
            value={periodEnd}
            onChange={(event) => {
              setPeriodEnd(event.target.value);
            }}
          />
        </label>
        <label className="form-label mb-0">
          Show
          <select
            className="form-select mt-1"
            value={only}
            onChange={(event) => {
              setOnly(event.target.value as Gap | "any" | "");
            }}
          >
            <option value="any">Controls with a gap</option>
            <option value="">All controls</option>
            {GAPS.map((gap) => (
              <option key={gap} value={gap}>
                {gapLabels[gap]}
              </option>
            ))}
          </select>
        </label>
      </form>
      {!ready ? (
        <p>Choose the start and end of the period.</p>
      ) : loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : !loaded.data ? (
        <Loading label="Loading evidence gaps…" />
      ) : (
        <>
          <ul aria-label="Gap summary" className="list-inline mb-2">
            {GAPS.map((gap) => (
              <li key={gap} className="list-inline-item">
                <Badge tone={gapTones[gap]}>
                  {`${gapLabels[gap]}: ${String(loaded.data?.summary[gap] ?? 0)}`}
                </Badge>
              </li>
            ))}
          </ul>
          <p className="text-secondary">
            {`${frameworkLabel(loaded.data.framework)} ${loaded.data.version}. `}
            {loaded.data.collected_at
              ? `Collected evidence from the evaluation run finished ${formatTime(loaded.data.collected_at)}.`
              : "No evaluation run had finished by the end of the period."}
          </p>
          {rows.length === 0 ? (
            <p>No controls to show.</p>
          ) : (
            <div className="table-responsive">
              <table className="table card-table" aria-label="Evidence gaps">
                <thead>
                  <tr>
                    <th>Control</th>
                    <th>Gaps</th>
                    <th>Why</th>
                    <th>Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <tr key={c.control_id}>
                      <td>
                        <Link to={`/controls/${loaded.data?.framework ?? ""}/${c.control_id}`}>
                          {c.control_id}
                        </Link>
                        <div className="text-secondary small">{c.title}</div>
                      </td>
                      <td>
                        {c.gaps.length === 0
                          ? c.status === "not_applicable"
                            ? "Not applicable"
                            : "None"
                          : c.gaps.map((gap) => (
                              <Badge key={gap} tone={gapTones[gap]}>
                                {gapLabels[gap]}
                              </Badge>
                            ))}
                      </td>
                      <td>{c.reasons.join("; ")}</td>
                      <td className="small">
                        {`${String(c.accepted_items)} accepted (${String(c.fresh_items)} fresh), ${String(c.collected_results)} collected results`}
                        {c.assessment_id !== null && (
                          <div>{`Assessment ${String(c.assessment_id)}`}</div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
