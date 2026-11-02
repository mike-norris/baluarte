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
import { linkClass } from "../components/styles";
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

const cell = "px-3 py-2";

const statusStyle: Record<CoverageStatus, string> = {
  covered: "bg-emerald-100 text-emerald-950",
  not_covered: "bg-red-100 text-red-950",
  unknown: "bg-amber-100 text-amber-950",
};

function TypeCell({ coverage }: { coverage: TypeCoverage | undefined }) {
  if (!coverage || coverage.status !== "ok") {
    const text = coverage?.status === "unsupported" ? "Not supported" : "Unknown";
    return (
      <span className="text-amber-900" title={text}>
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
                <Link to={securityPolicyHref(policy.id)} className={linkClass}>
                  {policy.name}
                </Link>
              ) : (
                policy.name
              )}
            </li>
          ))}
        </ul>
      ) : (
        <span className="text-slate-600">
          <span aria-hidden="true">✗ </span>
          {coverage.disabled > 0 ? `None enabled (${String(coverage.disabled)} disabled)` : "None"}
        </span>
      )}
      {coverage.truncated && <span className="block text-xs text-amber-900">List cut short</span>}
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
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{`GitLab security policy coverage by ${noun}`}</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
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
        <tbody className="divide-y divide-slate-100 align-top">
          {rows.map((row) => (
            <tr key={row.id}>
              <th scope="row" className={`${cell} font-medium`}>
                {row.path}
                {row.security_policy_project && (
                  <span className="block text-xs font-normal text-slate-600">
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
                <span className="mt-1 block text-slate-700">
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
    <div className="rounded border border-slate-200 bg-white p-3 text-sm">
      <h3 className="font-medium">{label}</h3>
      <p>
        {`${String(counts.covered)} covered · `}
        <span className={counts.not_covered > 0 ? "font-medium text-red-900" : ""}>
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

  const toggle = (active: boolean) =>
    `rounded px-3 py-1.5 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${
      active
        ? "bg-slate-900 text-white"
        : "border border-slate-300 bg-white text-slate-800 hover:bg-slate-100"
    }`;

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Counts label="Groups" counts={coverage.summary.groups} />
        <Counts label="Projects" counts={coverage.summary.projects} />
      </div>
      <div className="flex flex-wrap gap-4">
        <div role="group" aria-label="Show coverage for" className="flex gap-1">
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
        <div role="group" aria-label="Filter coverage" className="flex gap-1">
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
        <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
          {show === "gaps"
            ? `No ${kind} are known to be uncovered. Unknown ones are listed under "Only unknown".`
            : show === "unknown"
              ? `Coverage is known for every ${kind === "groups" ? "group" : "project"}.`
              : `No active ${kind} have been collected yet.`}
        </p>
      ) : (
        <CoverageTable rows={rows} kind={kind} groups={groups} />
      )}
      <p className="text-sm text-slate-600" aria-live="polite">
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
      <p role="status" className="text-slate-600">
        Loading coverage…
      </p>
    );
  } else body = <Body coverage={coverage.data} kind={kind} show={show} />;

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <SecurityPoliciesHeader context={coverage.data} />
      {body}
    </section>
  );
}
