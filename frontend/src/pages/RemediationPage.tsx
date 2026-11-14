import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import {
  CAN_PLAN,
  listPlans,
  planStates,
  type PlanPage,
  type PlanState,
  type PlanSummary,
} from "../api/remediation";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { DryRunNotice } from "../remediation/DryRunNotice";
import { hasPermission, isPlanState, planStateLabels, userLabel } from "../remediation/format";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { CapabilityGate } from "../ui/CapabilityGate";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

const stateTone: Record<PlanState, Tone> = {
  draft: "secondary",
  submitted: "info",
  approved: "primary",
  rejected: "danger",
  expired: "secondary",
};

function columns(user: CurrentUser | null): readonly Column<PlanSummary>[] {
  return [
    {
      key: "plan",
      header: "Plan",
      rowHeader: true,
      render: (plan) => (
        <Link to={`/remediation/${String(plan.id)}`} className="fw-medium">
          {`Plan ${String(plan.id)}`}
        </Link>
      ),
    },
    {
      key: "state",
      header: "State",
      render: (plan) => <Badge tone={stateTone[plan.state]}>{planStateLabels[plan.state]}</Badge>,
    },
    {
      key: "steps",
      header: "Steps",
      render: (plan) => plan.steps,
      numeric: true,
    },
    {
      key: "skipped",
      header: "Skipped",
      render: (plan) => plan.skipped,
      numeric: true,
      priority: 2,
    },
    {
      key: "by",
      header: "Created by",
      render: (plan) => userLabel(plan.created_by_user_id, user),
      priority: 2,
    },
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
}

function Filters({ state }: { state: PlanState | undefined }) {
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
      aria-label="Filter remediation plans"
      className="card-body border-bottom d-flex flex-wrap align-items-end gap-2"
    >
      <label className="form-label mb-0">
        State
        <select name="state" defaultValue={state ?? ""} className="form-select mt-1">
          <option value="">Any</option>
          {planStates.map((item) => (
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
        <Link to="/remediation" className="btn btn-ghost-secondary">
          Clear
        </Link>
      )}
    </form>
  );
}

function Listing({ state }: { state: PlanState | undefined }) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const key = state ?? "";
  const first = useLoad(`plans?${key}`, (signal) => listPlans(state, null, signal));
  const paged = usePaged<PlanSummary, PlanPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listPlans(state, after),
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
        <Loading label="Loading remediation plans…" />
      </div>
    );
  }
  if (paged.items.length === 0) {
    return (
      <EmptyState
        title={state ? "No plans are in this state." : "No remediation plans have been created."}
      />
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <>
      <ResponsiveTable
        caption="Remediation plans"
        columns={columns(user)}
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

export function RemediationPage() {
  const { session } = useAuth();
  const [params] = useSearchParams();
  const raw = params.get("state");
  const state = isPlanState(raw) ? raw : undefined;
  const canPlan = session.status === "signed_in" && hasPermission(session.user, CAN_PLAN);

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Act"
        title="Remediation"
        subtitle={`Plans of the GitLab changes that would fix open, failing findings, built from stored evidence. Someone other than a plan's creator must approve it, and every step is audited. "Expired" includes any plan past its expiry.`}
        actions={
          canPlan && (
            <CapabilityGate capability="write">
              <Link to="/remediation/new" className="btn btn-primary">
                Create a dry-run plan
              </Link>
            </CapabilityGate>
          )
        }
      />
      <DryRunNotice />
      {!canPlan && (
        <p className="text-secondary">
          Creating plans needs the remediation planning permission, which admins and analysts have.
        </p>
      )}
      <div className="card">
        <Filters state={state} />
        <Listing state={state} />
      </div>
    </section>
  );
}
