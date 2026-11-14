import { IconCircleCheck, IconCircleX, IconHelpCircle } from "@tabler/icons-react";
import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import {
  changeHref,
  changeKinds,
  changeOutcomes,
  CHANGES,
  listChanges,
  type Change,
  type ChangeFilters,
  type ChangePage,
  type ChangeKind,
  type ChangeOutcome,
} from "../api/changes";
import { projectHref } from "../api/gitlab";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { toLocal } from "../asof/format";
import {
  approvalsPart,
  changeName,
  changeOutcomeLabels,
  deploymentPart,
  formFilters,
  hasChangeFilters,
  kindLabels,
  pipelinePart,
  readChangeFilters,
  writeChangeFilters,
} from "../changes/format";
import { PartText } from "../changes/PartText";
import { ErrorMessage } from "../components/ErrorMessage";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { ListFooter } from "../ui/ListFooter";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

const outcomeTone: Record<ChangeOutcome, Tone> = {
  pass: "success",
  fail: "danger",
  unknown: "secondary",
};

const outcomeIcon = { pass: IconCircleCheck, fail: IconCircleX, unknown: IconHelpCircle } as const;

function text(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

function Filters({ filters }: { filters: ChangeFilters }) {
  const [params, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const next = writeChangeFilters(
      formFilters({
        project: text(form, "project"),
        kind: changeKinds.find((kind) => kind === text(form, "kind")) ?? "",
        from: text(form, "from"),
        to: text(form, "to"),
        outcome: changeOutcomes.find((outcome) => outcome === text(form, "outcome")) ?? "",
      }),
    );
    const asOf = params.get("as_of");
    if (asOf) next.set("as_of", asOf);
    setParams(next);
  }

  return (
    <form
      key={writeChangeFilters(filters).toString()}
      onSubmit={apply}
      aria-label="Filter changes"
      className="card-body border-bottom row g-2 align-items-end mx-0"
    >
      <label className="form-label col-6 col-md-4 col-xl mb-0">
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
      <label className="form-label col-6 col-md-4 col-xl mb-0">
        Kind
        <select name="kind" defaultValue={filters.kind ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {changeKinds.map((kind: ChangeKind) => (
            <option key={kind} value={kind}>
              {kindLabels[kind]}
            </option>
          ))}
        </select>
      </label>
      <label className="form-label col-6 col-md-4 col-xl mb-0">
        From (UTC)
        <input
          name="from"
          type="datetime-local"
          defaultValue={filters.from ? toLocal(filters.from) : ""}
          className="form-control mt-1"
        />
      </label>
      <label className="form-label col-6 col-md-4 col-xl mb-0">
        Before (UTC)
        <input
          name="to"
          type="datetime-local"
          defaultValue={filters.to ? toLocal(filters.to) : ""}
          className="form-control mt-1"
        />
      </label>
      <label className="form-label col-6 col-md-4 col-xl mb-0">
        Status
        <select name="outcome" defaultValue={filters.outcome ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {changeOutcomes.map((outcome) => (
            <option key={outcome} value={outcome}>
              {changeOutcomeLabels[outcome]}
            </option>
          ))}
        </select>
      </label>
      <div className="col-12 btn-list">
        <button type="submit" className="btn">
          Apply filters
        </button>
        {hasChangeFilters(filters) && (
          <Link to={CHANGES} className="btn btn-ghost-secondary">
            Clear
          </Link>
        )}
      </div>
    </form>
  );
}

const columns: readonly Column<Change>[] = [
  {
    key: "project",
    header: "Project",
    render: (change) => (
      <Link to={projectHref(change.project.gitlab_id)} className="text-break">
        {change.project.path}
      </Link>
    ),
    priority: 2,
  },
  {
    key: "change",
    header: "Change",
    rowHeader: true,
    render: (change) => (
      <>
        <Link to={changeHref(change.id)} className="fw-medium">
          {changeName(change)}
        </Link>
        <span className="d-block small text-secondary">{kindLabels[change.kind]}</span>
        {change.complete === false && (
          <span className="d-block small text-warning-emphasis">
            Partly read; it will be read again
          </span>
        )}
      </>
    ),
  },
  { key: "author", header: "Author", render: (change) => change.author ?? "Unknown", priority: 3 },
  {
    key: "when",
    header: "Merged or pushed",
    render: (change) => <span className="text-nowrap">{formatTime(change.changed_at)}</span>,
    priority: 2,
  },
  {
    key: "approvals",
    header: "Approvals",
    render: (change) => <PartText part={approvalsPart(change.approvals)} />,
    priority: 3,
  },
  {
    key: "pipeline",
    header: "Pipeline and tests",
    render: (change) => <PartText part={pipelinePart(change.pipeline)} />,
    priority: 3,
  },
  {
    key: "deployment",
    header: "Deployment",
    render: (change) => <PartText part={deploymentPart(change.deployments)} />,
    priority: 3,
  },
  {
    key: "status",
    header: "Status",
    render: (change) => (
      <>
        <Badge tone={outcomeTone[change.outcome] ?? "secondary"} icon={outcomeIcon[change.outcome]}>
          {changeOutcomeLabels[change.outcome] ?? change.outcome}
        </Badge>
        {change.reasons.map((reason) => (
          <span key={reason} className="d-block small text-secondary mt-1">
            {reason}
          </span>
        ))}
      </>
    ),
  },
];

function Listing({ filters }: { filters: ChangeFilters }) {
  const asOf = useAsOf();
  const key = `${writeChangeFilters(filters).toString()}@${asOf ?? "now"}`;
  const first = useLoad(`changes?${key}`, (signal) => listChanges(filters, null, asOf, signal));
  const paged = usePaged<Change, ChangePage>(
    key,
    first,
    (page) => page.next_before_id,
    (before) => listChanges(filters, before, asOf),
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
        <Loading label="Loading changes…" />
      </div>
    );
  }
  return (
    <>
      <div className="card-body pb-0">
        <AsOfResult answer={first.data} />
      </div>
      {paged.items.length === 0 ? (
        <EmptyState
          title={
            hasChangeFilters(filters)
              ? "No changes match these filters."
              : "No changes recorded yet. Changes are read from each monitored project's default branch, from the first collection back over the configured lookback."
          }
        />
      ) : (
        <ResponsiveTable
          caption="Changes, newest first"
          columns={columns}
          rows={paged.items}
          rowKey={(change) => change.id}
        />
      )}
      {paged.items.length > 0 || paged.next !== null ? (
        <ListFooter
          noun="changes"
          shown={paged.items.length}
          hasMore={paged.next !== null}
          pending={paged.pending}
          error={paged.moreError}
          onMore={() => void paged.loadMore()}
          moreLabel="Load older changes"
        />
      ) : null}
    </>
  );
}

export function ChangesPage() {
  const [params] = useSearchParams();
  const filters = readChangeFilters(params);
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Observe"
        title="Changes"
        subtitle="Merge requests merged into each monitored project’s default branch, and commits pushed to it directly, with who approved them, their pipeline and tests, and where they were deployed. Anything that could not be read is unknown, never counted as reviewed or passed."
      />
      <div className="card">
        <Filters filters={filters} />
        <Listing filters={filters} />
      </div>
    </section>
  );
}
