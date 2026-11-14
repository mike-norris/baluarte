import { useId, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  CAN_PLAN,
  createLabelPlan,
  labelPlanStates,
  listLabelPlans,
  type LabelPlanPage,
  type LabelPlanState,
  type LabelPlanSummary,
} from "../api/labels";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { useAsOfState } from "../asof/context";
import { PastReadOnly } from "../asof/PastReadOnly";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  isPlanState,
  LABEL_PLANS,
  LABELS,
  labelPlanHref,
  parseAliases,
  planStateLabels,
} from "../labels/format";
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

const columns: readonly Column<LabelPlanSummary>[] = [
  {
    key: "plan",
    header: "Plan",
    rowHeader: true,
    render: (plan) => (
      <Link to={labelPlanHref(plan.id)} className="fw-medium">
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
      plan.scope.groups.length > 0 ? plan.scope.groups.join(", ") : "Whole taxonomy",
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

/** Build a plan: Baluarte reads the scope's labels again first, so the plan starts current. */
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
      const plan = await createLabelPlan(parseAliases(groups), csrf);
      void navigate(labelPlanHref(plan.id), { state: { created: true } });
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
          Baluarte reads the labels of the groups and projects in scope from GitLab now (read only),
          then lists the creates, renames and updates that would fix the drift. Duplicates,
          misplaced labels and labels not in the taxonomy are listed for you to fix by hand; nothing
          is ever deleted.
        </p>
        <label className="form-label" htmlFor={`${id}-groups`}>
          Top-level groups (comma-separated; leave empty for the taxonomy's whole scope)
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
          {sending ? "Reading labels and building the plan…" : "Create a plan"}
        </button>
      </div>
    </form>
  );
}

function Filters({ state }: { state: LabelPlanState | undefined }) {
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
      aria-label="Filter label plans"
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
        <Link to={LABEL_PLANS} className="btn btn-ghost-secondary">
          Clear
        </Link>
      )}
    </form>
  );
}

function Listing({ state }: { state: LabelPlanState | undefined }) {
  const key = state ?? "";
  const first = useLoad(`labels:plans?${key}`, (signal) => listLabelPlans(state, null, signal));
  const paged = usePaged<LabelPlanSummary, LabelPlanPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listLabelPlans(state, after),
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
        <Loading label="Loading label plans…" />
      </div>
    );
  }
  if (paged.items.length === 0) {
    return (
      <EmptyState
        title={state ? "No plans are in this state." : "No label plans have been created."}
      />
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <>
      <ResponsiveTable
        caption="Label plans"
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

export function LabelPlansPage() {
  const [params] = useSearchParams();
  const raw = params.get("state");
  const state = isPlanState(raw) ? raw : undefined;
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to={LABELS}>Labels</Link>}
        title="Label plans"
        subtitle="Plans of the label changes that would bring groups and projects in line with the taxonomy. A plan is applied only after approval, with a separate write token, one step at a time, each read back to verify it. Labels are created, renamed or updated, never deleted."
      />
      <CreatePlan />
      <div className="card">
        <Filters state={state} />
        <Listing state={state} />
      </div>
    </section>
  );
}
