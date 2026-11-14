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
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { ErrorMessage } from "../components/ErrorMessage";
import { ExportLink } from "../evidence/ExportLink";
import { findingsExportLink } from "../evidence/scope";
import { findingsPlanLink } from "../remediation/format";
import { PlanLink } from "../remediation/PlanLink";
import { EvaluationNote } from "../findings/EvaluationNote";
import { hasFindingFilters, readFindingFilters, writeFindingFilters } from "../findings/filters";
import { FindingStateBadge, OutcomeBadge, SeverityBadge } from "../findings/badges";
import { outcomeLabels, severityLabels } from "../findings/format";
import { formatTime } from "../time";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";
import { ListFooter } from "../ui/ListFooter";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

function withoutTag(filters: FindingFilters): FindingFilters {
  const rest = { ...filters };
  delete rest.tag;
  return rest;
}

function Filters({ filters }: { filters: FindingFilters }) {
  const [params, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const query = new URLSearchParams();
    for (const field of ["state", "outcome", "severity", "project", "policy", "rule"]) {
      const value = form.get(field);
      if (typeof value === "string" && value.trim()) query.set(field, value.trim());
    }
    // The rule type and tag come from links (such as Labels and Manager), not the form; keep them.
    if (filters.rule_type) query.set("rule_type", filters.rule_type);
    if (filters.tag) query.set("tag", filters.tag);
    const next = writeFindingFilters(readFindingFilters(query));
    const asOf = params.get("as_of");
    if (asOf) next.set("as_of", asOf);
    setParams(next);
  }

  return (
    // Remount when the URL changes so the fields follow navigation.
    <form
      key={writeFindingFilters(filters).toString()}
      onSubmit={apply}
      aria-label="Filter findings"
      className="card-body border-bottom row g-2 align-items-end mx-0"
    >
      <label className="form-label col-6 col-md-4 col-xl-2 mb-0">
        State
        <select name="state" defaultValue={filters.state ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
          <option value="excepted">Excepted</option>
        </select>
      </label>
      <label className="form-label col-6 col-md-4 col-xl-2 mb-0">
        Latest outcome
        <select name="outcome" defaultValue={filters.outcome ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {outcomes.map((outcome) => (
            <option key={outcome} value={outcome}>
              {outcomeLabels[outcome]}
            </option>
          ))}
        </select>
      </label>
      <label className="form-label col-6 col-md-4 col-xl-2 mb-0">
        Severity
        <select name="severity" defaultValue={filters.severity ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {severities.map((severity) => (
            <option key={severity} value={severity}>
              {severityLabels[severity]}
            </option>
          ))}
        </select>
      </label>
      <label className="form-label col-6 col-md-4 col-xl-2 mb-0">
        Policy name
        <input
          name="policy"
          defaultValue={filters.policy ?? ""}
          maxLength={63}
          className="form-control mt-1"
        />
      </label>
      <label className="form-label col-6 col-md-4 col-xl-2 mb-0">
        Rule ID
        <input
          name="rule"
          defaultValue={filters.rule ?? ""}
          maxLength={63}
          className="form-control mt-1"
        />
      </label>
      <label className="form-label col-6 col-md-4 col-xl-2 mb-0">
        Project GitLab ID
        <input
          name="project"
          type="number"
          min={1}
          step={1}
          defaultValue={filters.project ?? ""}
          className="form-control mt-1"
        />
      </label>
      {filters.tag && (
        <p className="col-12 mb-0">
          {`Projects tagged ${filters.tag.replace(":", "=")} only. `}
          <Link to={`?${writeFindingFilters(withoutTag(filters)).toString()}`}>
            Remove the tag filter
          </Link>
        </p>
      )}
      <div className="col-12 btn-list">
        <button type="submit" className="btn">
          Apply filters
        </button>
        {hasFindingFilters(filters) && (
          <Link to="/findings" className="btn btn-ghost-secondary">
            Clear
          </Link>
        )}
      </div>
    </form>
  );
}

function StateCell({ finding }: { finding: Finding }) {
  return (
    <>
      <FindingStateBadge state={finding.state} />
      {finding.state === "excepted" && finding.exception && (
        <span className="d-block small text-secondary">
          {`until ${formatTime(finding.exception.expires_at)}; not passing`}
        </span>
      )}
      {finding.state === "resolved" && finding.resolution === "project_archived" && (
        <span className="d-block small text-secondary">project archived; not checked</span>
      )}
    </>
  );
}

const columns: readonly Column<Finding>[] = [
  {
    key: "project",
    header: "Project",
    rowHeader: true,
    render: (finding) => (
      <Link to={`/findings/${String(finding.id)}`} className="fw-medium text-break">
        {finding.project.path}
      </Link>
    ),
  },
  { key: "rule", header: "Rule", render: (finding) => finding.rule_id },
  {
    key: "policy",
    header: "Policy",
    render: (finding) => (
      <>
        {finding.policy.name}{" "}
        <span className="text-secondary">{`v${String(finding.policy.version)}`}</span>
      </>
    ),
    priority: 3,
  },
  {
    key: "severity",
    header: "Severity",
    render: (finding) => <SeverityBadge severity={finding.severity} />,
  },
  { key: "state", header: "State", render: (finding) => <StateCell finding={finding} /> },
  {
    key: "outcome",
    header: "Latest outcome",
    render: (finding) => <OutcomeBadge outcome={finding.latest_outcome} />,
    priority: 2,
  },
  {
    key: "first",
    header: "First seen",
    render: (finding) => <span className="text-nowrap">{formatTime(finding.first_seen_at)}</span>,
    priority: 3,
  },
  {
    key: "last",
    header: "Last seen",
    render: (finding) => <span className="text-nowrap">{formatTime(finding.last_seen_at)}</span>,
    priority: 2,
  },
];

function Listing({ filters }: { filters: FindingFilters }) {
  const asOf = useAsOf();
  const key = `${writeFindingFilters(filters).toString()}@${asOf ?? "now"}`;
  const first = useLoad(`findings?${key}`, (signal) => listFindings(filters, null, signal, asOf));
  const paged = usePaged<Finding, FindingPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listFindings(filters, after, undefined, asOf),
  );

  if (first.error) {
    return (
      <div className="card-body">
        <ErrorMessage error={first.error} retry={first.retry} />
      </div>
    );
  }
  if (first.loading) {
    return (
      <div className="card-body">
        <Loading label="Loading findings…" />
      </div>
    );
  }
  if (paged.items.length === 0) {
    return (
      <>
        <div className="card-body pb-0">
          <AsOfResult answer={first.data} />
        </div>
        <EmptyState
          title={
            hasFindingFilters(filters)
              ? "No findings match these filters."
              : "No findings yet. A finding opens when a rule fails; it does not mean every rule passed."
          }
        />
      </>
    );
  }
  return (
    <>
      <div className="card-body pb-0">
        <AsOfResult answer={first.data} />
      </div>
      <ResponsiveTable
        caption="Findings"
        columns={columns}
        rows={paged.items}
        rowKey={(finding) => finding.id}
      />
      <ListFooter
        noun="findings"
        shown={paged.items.length}
        hasMore={paged.next !== null}
        pending={paged.pending}
        error={paged.moreError}
        onMore={() => void paged.loadMore()}
      />
    </>
  );
}

export function FindingsPage() {
  const [params] = useSearchParams();
  const filters = readFindingFilters(params);
  const planLink = findingsPlanLink(filters);
  const device = useDeviceClass();

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Observe"
        title="Findings"
        subtitle="Rules a project failed, followed until they pass. An unknown or unsupported latest outcome means the last check could not decide; it never resolves a finding."
      />
      <EvaluationNote />
      <div className="d-flex flex-column gap-1 mb-3">
        <ExportLink label="Export evidence for this view" {...findingsExportLink(filters)} />
        {planLink && device !== "phone" && (
          <PlanLink label="Plan a dry-run fix for this view" {...planLink} />
        )}
      </div>
      <div className="card">
        <Filters filters={filters} />
        <Listing filters={filters} />
      </div>
    </section>
  );
}
