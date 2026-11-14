import { Link, useSearchParams } from "react-router";

import {
  getCoverage,
  policyTypes,
  type Coverage,
  type CoverageCounts,
  type CoverageRow,
  type CoverageStatus,
  type TypeCoverage,
} from "../api/securityPolicies";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  coverageLabels,
  coverageReason,
  securityPolicyHref,
  shortTypeLabels,
  typeLabels,
} from "../security-policies/format";
import { SecurityPoliciesHeader } from "../security-policies/parts";

type Kind = "groups" | "projects";
type Show = "all" | "gaps" | "unknown";

const cell = "align-top";

const statusStyle: Record<CoverageStatus, string> = {
  covered: "bg-success-subtle text-success-emphasis border border-success-subtle",
  not_covered: "bg-danger-subtle text-danger-emphasis border border-danger-subtle",
  unknown: "bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle",
};

function TypeCell({ coverage }: { coverage: TypeCoverage | undefined }) {
  if (!coverage || coverage.status !== "ok") {
    const text = coverage?.status === "unsupported" ? "Not supported" : "Unknown";
    return (
      <span className="text-warning-emphasis" title={text}>
        <span aria-hidden="true">? </span>
        {text}
      </span>
    );
  }
  const enabled = coverage.policies.filter((policy) => policy.enabled === true);
  return (
    <>
      {enabled.length > 0 ? (
        <ul>
          {enabled.map((policy) => (
            <li key={`${String(policy.id)}:${policy.name}`}>
              <span aria-hidden="true">✓ </span>
              {policy.id !== null ? (
                <Link to={securityPolicyHref(policy.id)}>{policy.name}</Link>
              ) : (
                policy.name
              )}
            </li>
          ))}
        </ul>
      ) : (
        <span className="text-secondary">
          <span aria-hidden="true">✗ </span>
          {coverage.disabled > 0 ? `None enabled (${String(coverage.disabled)} disabled)` : "None"}
        </span>
      )}
      {coverage.truncated && (
        <span className="d-block small text-warning-emphasis">List cut short</span>
      )}
    </>
  );
}

function CoverageTable({
  rows,
  kind,
  groups,
}: {
  rows: CoverageRow[];
  kind: Kind;
  groups: Map<string, CoverageRow>;
}) {
  const noun = kind === "groups" ? "group" : "project";
  return (
    <div className="card table-responsive">
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">{`GitLab security policy coverage by ${noun}`}</caption>
        <thead>
          <tr>
            <th scope="col" className={`${cell} font-medium`}>
              {kind === "groups" ? "Group" : "Project"}
            </th>
            <th scope="col" className={`${cell} font-medium`}>
              Coverage
            </th>
            {policyTypes.map((type) => (
              <th key={type} scope="col" className={`${cell} font-medium`}>
                <abbr title={typeLabels[type]} className="no-underline">
                  {shortTypeLabels[type]}
                </abbr>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="align-top">
          {rows.map((row) => (
            <tr key={row.id}>
              <th scope="row" className={`${cell} font-medium`}>
                {row.path}
                {row.security_policy_project && (
                  <span className="d-block small fw-normal text-secondary">
                    {`Policy source project: ${row.security_policy_project}`}
                  </span>
                )}
              </th>
              <td className={`${cell} min-w-64`}>
                <span
                  className={`rounded px-1.5 py-0.5 text-xs font-medium ${statusStyle[row.status]}`}
                >
                  {coverageLabels[row.status]}
                </span>
                <span className="mt-1 d-block text-secondary">
                  {coverageReason(row, kind === "groups" ? "group" : "project", groups)}
                </span>
              </td>
              {policyTypes.map((type) => (
                <td key={type} className={cell}>
                  <TypeCell coverage={row.types[type]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Counts({ label, counts }: { label: string; counts: CoverageCounts }) {
  return (
    <div className="rounded border p-3">
      <h3 className="fw-medium">{label}</h3>
      <p>
        {`${String(counts.covered)} covered · `}
        <span className={counts.not_covered > 0 ? "fw-medium text-danger-emphasis" : ""}>
          {`${String(counts.not_covered)} not covered`}
        </span>
        {` · ${String(counts.unknown)} unknown`}
      </p>
    </div>
  );
}

function readView(params: URLSearchParams): { kind: Kind; show: Show } {
  const kind = params.get("kind") === "projects" ? "projects" : "groups";
  const raw = params.get("show");
  const show: Show = raw === "gaps" || raw === "unknown" ? raw : "all";
  return { kind, show };
}

function Body({ coverage, kind, show }: { coverage: Coverage; kind: Kind; show: Show }) {
  const [, setParams] = useSearchParams();
  const groups = new Map(coverage.groups.map((row) => [row.path, row]));
  const all = coverage[kind];
  const rows =
    show === "gaps"
      ? all.filter((row) => row.status === "not_covered")
      : show === "unknown"
        ? all.filter((row) => row.status === "unknown")
        : all;

  function set(next: { kind?: Kind; show?: Show }) {
    const params = new URLSearchParams();
    const merged = { kind, show, ...next };
    if (merged.kind !== "groups") params.set("kind", merged.kind);
    if (merged.show !== "all") params.set("show", merged.show);
    setParams(params);
  }

  const toggle = (active: boolean) => `btn btn-sm ${active ? "btn-primary" : ""}`;

  return (
    <div>
      <div className="row g-3 row-cols-1 row-cols-sm-2 mb-3">
        <Counts label="Groups" counts={coverage.summary.groups} />
        <Counts label="Projects" counts={coverage.summary.projects} />
      </div>
      <div className="d-flex flex-wrap gap-3">
        <div role="group" aria-label="Show coverage for" className="d-flex gap-1">
          {(["groups", "projects"] as const).map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={kind === value}
              onClick={() => set({ kind: value })}
              className={toggle(kind === value)}
            >
              {value === "groups" ? "Groups" : "Projects"}
            </button>
          ))}
        </div>
        <div role="group" aria-label="Filter coverage" className="d-flex gap-1">
          {(
            [
              ["all", "All"],
              ["gaps", "Only gaps"],
              ["unknown", "Only unknown"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={show === value}
              onClick={() => set({ show: value })}
              className={toggle(show === value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {rows.length === 0 ? (
        <p className="text-secondary border border-dashed rounded p-3">
          {show === "gaps"
            ? `No ${kind} are known to be uncovered. Unknown ones are listed under "Only unknown".`
            : show === "unknown"
              ? `Coverage is known for every ${kind === "groups" ? "group" : "project"}.`
              : `No active ${kind} have been collected yet.`}
        </p>
      ) : (
        <CoverageTable rows={rows} kind={kind} groups={groups} />
      )}
      <p className="text-secondary" aria-live="polite">
        {`Showing ${String(rows.length)} of ${String(all.length)} ${kind}.`}
      </p>
    </div>
  );
}

export function SecurityPolicyCoveragePage() {
  const [params] = useSearchParams();
  const { kind, show } = readView(params);
  const coverage = useLoad("security-policy-coverage", getCoverage);

  let body;
  if (coverage.error) body = <ErrorMessage error={coverage.error} retry={coverage.retry} />;
  else if (!coverage.data) {
    body = (
      <p role="status" className="text-secondary">
        Loading coverage…
      </p>
    );
  } else body = <Body coverage={coverage.data} kind={kind} show={show} />;

  return (
    <section aria-labelledby="page-title">
      <SecurityPoliciesHeader context={coverage.data} />
      {body}
    </section>
  );
}
