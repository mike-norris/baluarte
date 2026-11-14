import { useId, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  createIssuePlan,
  durationText,
  getIssuePreview,
  issuePlanHref,
  ISSUE_PLANS,
  listIssuePlans,
  type IssuePlanPage,
  type IssuePlanState,
  type IssuePlanSummary,
} from "../api/issuePlans";
import { CAN_PLAN, labelPlanStates } from "../api/labels";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { useAsOfState } from "../asof/context";
import { PastReadOnly } from "../asof/PastReadOnly";
import { ErrorMessage } from "../components/ErrorMessage";
import { isPlanState, parseAliases, planStateLabels } from "../labels/format";
import { personText } from "../policies/format";
import { hasPermission } from "../remediation/format";
import { planStateTone } from "../plans/format";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { LARGER_SCREEN } from "../ui/CapabilityGate";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

const columns: readonly Column<IssuePlanSummary>[] = [
  {
    key: "plan",
    header: "Plan",
    rowHeader: true,
    render: (plan) => (
      <Link to={issuePlanHref(plan.id)} className="fw-medium">
        {`Plan ${String(plan.id)}`}
      </Link>
    ),
  },
  {
    key: "state",
    header: "State",
    render: (plan) => <Badge tone={planStateTone[plan.state]}>{planStateLabels[plan.state]}</Badge>,
  },
  {
    key: "scope",
    header: "Scope",
    render: (plan) =>
      plan.scope.groups.length > 0 ? plan.scope.groups.join(", ") : "All projects",
    priority: 2,
  },
  { key: "steps", header: "Steps", render: (plan) => plan.steps, numeric: true },
  { key: "manual", header: "By hand", render: (plan) => plan.manual, numeric: true, priority: 2 },
  { key: "by", header: "Created by", render: (plan) => personText(plan.created_by), priority: 3 },
  {
    key: "created",
    header: "Created",
    render: (plan) => <span className="text-nowrap">{formatTime(plan.created_at)}</span>,
    priority: 3,
  },
  {
    key: "expires",
    header: "Expires",
    render: (plan) => <span className="text-nowrap">{formatTime(plan.expires_at)}</span>,
  },
];

/** What a plan would change now, from the closed issues as last collected. */
function Preview() {
  const loaded = useLoad("issue-plans:preview", (signal) => getIssuePreview(null, signal));
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Loading label="Loading closed issues…" />;
  const preview = loaded.data;
  const rules = preview.rules;
  return (
    <section aria-labelledby="rules-title" className="card mb-3">
      <div className="card-header">
        <h2 id="rules-title" className="card-title">
          Rules
        </h2>
      </div>
      <div className="card-body">
        <ul className="ps-3">
          <li>
            {`A closed issue with commits (a merge request, or a commit mentioning it) and no time spent gets the label "${rules.missing_time_label}".`}
          </li>
          <li>{`Once time is logged on it, the label is removed.`}</li>
          <li>
            {`A closed issue with time spent and no estimate gets the time spent, rounded up to ${durationText(rules.estimate_round_minutes * 60)}, as its estimate. An estimate is never changed.`}
          </li>
          <li>{`Issue types covered: ${rules.issue_types.join(", ")}.`}</li>
        </ul>
        <dl className="row g-2 mb-3">
          {(
            [
              ["Closed issues read", preview.closed_issues],
              ["Labels to add", preview.add_label],
              ["Labels to remove", preview.remove_label],
              ["Estimates to set", preview.set_estimate],
            ] as const
          ).map(([term, value]) => (
            <div key={term} className="col-6 col-sm-3">
              <div className="border rounded p-2 h-100">
                <dt className="subheader">{term}</dt>
                <dd className="h2 mb-0 tabular-nums">{value}</dd>
              </div>
            </div>
          ))}
        </dl>
        <p className="text-secondary mb-0">
          {preview.last_read_at
            ? `Closed issues last read ${formatTime(preview.last_read_at)}. `
            : "Closed issues have not been read yet; they are read with each time collection. "}
          {preview.commits_unknown > 0 &&
            `${String(preview.commits_unknown)} issues without time are waiting for their commits to be checked. `}
          {preview.writes_enabled
            ? "Issue writes are allowed for this deployment."
            : "Issue writes are turned off for this deployment (BALUARTE_ISSUE_WRITES), so plans can be built and reviewed but not applied."}
        </p>
      </div>
    </section>
  );
}

function CreatePlan() {
  const { session, refresh } = useAuth();
  const { chosen } = useAsOfState();
  const device = useDeviceClass();
  const navigate = useNavigate();
  const id = useId();
  const [groups, setGroups] = useState("");
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const user = session.status === "signed_in" ? session.user : null;
  if (!user || !hasPermission(user, CAN_PLAN)) {
    return (
      <p className="text-secondary">
        Creating plans needs the remediation planning permission, which admins and analysts have.
      </p>
    );
  }
  if (chosen !== null) return <PastReadOnly />;
  if (device === "phone") {
    return <p className="text-secondary">{`Creating a plan: ${LARGER_SCREEN}`}</p>;
  }
  const csrf = user.csrf_token;

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSending(true);
    setFailure(null);
    try {
      const plan = await createIssuePlan(parseAliases(groups), csrf);
      void navigate(issuePlanHref(plan.id), { state: { created: true } });
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void create(event)}
      aria-labelledby={`${id}-title`}
      className="card mb-3"
    >
      <div className="card-header">
        <h2 id={`${id}-title`} className="card-title">
          Create a plan
        </h2>
      </div>
      <div className="card-body">
        <p className="text-secondary">
          Baluarte lists the changes from the closed issues as last read, and checks with GitLab
          (read only) which projects define the label. Nothing changes in GitLab until the plan is
          approved and applied.
        </p>
        <label className="form-label" htmlFor={`${id}-groups`}>
          Groups (comma-separated; leave empty for every monitored project)
          <input
            id={`${id}-groups`}
            value={groups}
            onChange={(event) => {
              setGroups(event.target.value);
            }}
            className="form-control mt-1"
          />
        </label>
        {failure && (
          <p role="alert" className="alert alert-danger text-body">
            {failure}
          </p>
        )}
      </div>
      <div className="card-footer text-end">
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Building the plan…" : "Create a plan"}
        </button>
      </div>
    </form>
  );
}

function Filters({ state }: { state: IssuePlanState | undefined }) {
  const [, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = new FormData(event.currentTarget).get("state");
    setParams(typeof value === "string" && isPlanState(value) ? { state: value } : {});
  }

  return (
    <form
      key={state ?? ""}
      onSubmit={apply}
      aria-label="Filter issue plans"
      className="card-body border-bottom d-flex flex-wrap align-items-end gap-2"
    >
      <label className="form-label mb-0">
        State
        <select name="state" defaultValue={state ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {labelPlanStates.map((item) => (
            <option key={item} value={item}>
              {planStateLabels[item]}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" className="btn">
        Apply filter
      </button>
      {state && (
        <Link to={ISSUE_PLANS} className="btn btn-ghost-secondary">
          Clear
        </Link>
      )}
    </form>
  );
}

function Listing({ state }: { state: IssuePlanState | undefined }) {
  const key = state ?? "";
  const first = useLoad(`issue-plans?${key}`, (signal) => listIssuePlans(state, null, signal));
  const paged = usePaged<IssuePlanSummary, IssuePlanPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listIssuePlans(state, after),
  );

  if (first.error) {
    return (
      <div className="card-body">
        <ErrorMessage error={first.error} retry={first.retry} />
      </div>
    );
  }
  if (!first.data) {
    return (
      <div className="card-body">
        <Loading label="Loading issue plans…" />
      </div>
    );
  }
  if (paged.items.length === 0) {
    return (
      <EmptyState
        title={state ? "No plans are in this state." : "No issue plans have been created."}
      />
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <>
      <ResponsiveTable
        caption="Issue plans"
        columns={columns}
        rows={paged.items}
        rowKey={(plan) => plan.id}
      />
      <div className="card-footer d-flex flex-wrap align-items-center gap-2">
        <p className="text-secondary mb-0 me-auto" aria-live="polite">
          {`Showing ${String(paged.items.length)} plans${more}.`}
        </p>
        {paged.next !== null && (
          <button
            type="button"
            onClick={() => void paged.loadMore()}
            disabled={paged.pending}
            className="btn"
          >
            {paged.pending ? "Loading…" : "Load more plans"}
          </button>
        )}
      </div>
      {paged.moreError && (
        <div className="card-body">
          <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
        </div>
      )}
    </>
  );
}

export function IssuePlansPage() {
  const [params] = useSearchParams();
  const raw = params.get("state");
  const state = isPlanState(raw) ? raw : undefined;
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Act"
        title="Closed issue time"
        subtitle="Closed issues should account for their time. A plan flags issues closed with commits but no time spent, and gives issues without an estimate one from their time spent. It is applied only after approval, with the separate write token, one issue at a time, each read back to verify it."
      />
      <Preview />
      <CreatePlan />
      <div className="card">
        <Filters state={state} />
        <Listing state={state} />
      </div>
    </section>
  );
}
