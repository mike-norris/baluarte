import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import {
  listFindings,
  outcomes,
  severities,
  type Finding,
  type FindingFilters,
  type FindingPage,
} from "../api/evaluations";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { ExportLink } from "../evidence/ExportLink";
import { findingsExportLink } from "../evidence/scope";
import { EvaluationNote } from "../findings/EvaluationNote";
import {
  hasFindingFilters,
  readFindingFilters,
  writeFindingFilters,
} from "../findings/filters";
import {
  findingStateLabels,
  outcomeLabels,
  outcomeMeaning,
  severityLabels,
} from "../findings/format";
import { formatTime } from "../time";

const cell = "px-3 py-2";

function Filters({ filters }: { filters: FindingFilters }) {
  const [, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const query = new URLSearchParams();
    for (const field of ["state", "outcome", "severity", "project", "policy", "rule"]) {
      const value = form.get(field);
      if (typeof value === "string" && value.trim()) query.set(field, value.trim());
    }
    setParams(writeFindingFilters(readFindingFilters(query)));
  }

  return (
    // Remount when the URL changes so the fields follow navigation.
    <form
      key={writeFindingFilters(filters).toString()}
      onSubmit={apply}
      aria-label="Filter findings"
      className="grid gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-2 lg:grid-cols-6 lg:items-end"
    >
      <label className="text-sm font-medium">
        State
        <select name="state" defaultValue={filters.state ?? ""} className={fieldClass}>
          <option value="">Any</option>
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
          <option value="excepted">Excepted</option>
        </select>
      </label>
      <label className="text-sm font-medium">
        Latest outcome
        <select name="outcome" defaultValue={filters.outcome ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {outcomes.map((outcome) => (
            <option key={outcome} value={outcome}>
              {outcomeLabels[outcome]}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm font-medium">
        Severity
        <select name="severity" defaultValue={filters.severity ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {severities.map((severity) => (
            <option key={severity} value={severity}>
              {severityLabels[severity]}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm font-medium">
        Policy name
        <input
          name="policy"
          defaultValue={filters.policy ?? ""}
          maxLength={63}
          className={fieldClass}
        />
      </label>
      <label className="text-sm font-medium">
        Rule ID
        <input name="rule" defaultValue={filters.rule ?? ""} maxLength={63} className={fieldClass} />
      </label>
      <label className="text-sm font-medium">
        Project GitLab ID
        <input
          name="project"
          type="number"
          min={1}
          step={1}
          defaultValue={filters.project ?? ""}
          className={fieldClass}
        />
      </label>
      <div className="flex gap-2 sm:col-span-2 lg:col-span-6">
        <button type="submit" className={buttonClass}>
          Apply filters
        </button>
        {hasFindingFilters(filters) && (
          <Link to="/findings" className={secondaryClass}>
            Clear
          </Link>
        )}
      </div>
    </form>
  );
}

function OutcomeCell({ finding }: { finding: Finding }) {
  const doubtful = finding.latest_outcome === "unknown" || finding.latest_outcome === "unsupported";
  return (
    <td className={cell}>
      {outcomeLabels[finding.latest_outcome]}
      {doubtful && <span className="sr-only">: {outcomeMeaning[finding.latest_outcome]}</span>}
    </td>
  );
}

function FindingRows({ findings }: { findings: Finding[] }) {
  return findings.map((finding) => (
    <tr key={finding.id}>
      <th scope="row" className={`${cell} font-medium`}>
        <Link to={`/findings/${String(finding.id)}`} className={linkClass}>
          {finding.project.path}
        </Link>
      </th>
      <td className={cell}>{finding.rule_id}</td>
      <td className={cell}>
        {finding.policy.name}{" "}
        <span className="text-slate-600">{`v${String(finding.policy.version)}`}</span>
      </td>
      <td className={cell}>{severityLabels[finding.severity] ?? finding.severity}</td>
      <td className={cell}>
        {findingStateLabels[finding.state]}
        {finding.state === "excepted" && finding.exception && (
          <span className="block text-xs text-slate-600">
            {`until ${formatTime(finding.exception.expires_at)}; not passing`}
          </span>
        )}
      </td>
      <OutcomeCell finding={finding} />
      <td className={`${cell} whitespace-nowrap`}>{formatTime(finding.first_seen_at)}</td>
      <td className={`${cell} whitespace-nowrap`}>{formatTime(finding.last_seen_at)}</td>
    </tr>
  ));
}

const headers = [
  "Project",
  "Rule",
  "Policy",
  "Severity",
  "State",
  "Latest outcome",
  "First seen",
  "Last seen",
];

function Listing({ filters }: { filters: FindingFilters }) {
  const key = writeFindingFilters(filters).toString();
  const first = useLoad(`findings?${key}`, (signal) => listFindings(filters, null, signal));
  const paged = usePaged<Finding, FindingPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listFindings(filters, after),
  );

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (first.loading) {
    return (
      <p role="status" className="text-slate-600">
        Loading findings…
      </p>
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  if (paged.items.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {hasFindingFilters(filters)
          ? "No findings match these filters."
          : "No findings yet. A finding opens when a rule fails; " +
            "it does not mean every rule passed."}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Findings</caption>
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
            <FindingRows findings={paged.items} />
          </tbody>
        </table>
      </div>
      <p className="text-sm text-slate-600" aria-live="polite">
        {`Showing ${String(paged.items.length)} findings${more}.`}
      </p>
      {paged.moreError && (
        <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
      )}
      {paged.next !== null && (
        <button
          type="button"
          onClick={() => void paged.loadMore()}
          disabled={paged.pending}
          className={secondaryClass}
        >
          {paged.pending ? "Loading…" : "Load more findings"}
        </button>
      )}
    </div>
  );
}

export function FindingsPage() {
  const [params] = useSearchParams();
  const filters = readFindingFilters(params);

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Findings
        </h1>
        <p className="mt-2 text-slate-600">
          Rules a project failed, followed until they pass. An unknown or unsupported latest
          outcome means the last check could not decide; it never resolves a finding.
        </p>
      </div>
      <EvaluationNote />
      <Filters filters={filters} />
      <ExportLink label="Export evidence for this view" {...findingsExportLink(filters)} />
      <Listing filters={filters} />
    </section>
  );
}
