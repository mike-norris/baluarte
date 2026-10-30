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
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { DryRunNotice } from "../remediation/DryRunNotice";
import { hasPermission, isPlanState, planStateLabels, userLabel } from "../remediation/format";
import { formatTime } from "../time";

const cell = "px-3 py-2";
const headers = ["Plan", "State", "Steps", "Skipped", "Created by", "Created", "Expires"];

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
      className="flex flex-wrap items-end gap-3 rounded border border-slate-200 bg-white p-4"
    >
      <label className="text-sm font-medium">
        State
        <select name="state" defaultValue={state ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {planStates.map((item) => (
            <option key={item} value={item}>
              {planStateLabels[item]}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" className={buttonClass}>
        Apply filter
      </button>
      {state && (
        <Link to="/remediation" className={secondaryClass}>
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

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (first.loading) {
    return (
      <p role="status" className="text-slate-600">
        Loading remediation plans…
      </p>
    );
  }
  if (paged.items.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {state ? "No plans are in this state." : "No remediation plans have been created."}
      </p>
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Remediation plans</caption>
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
            {paged.items.map((plan) => (
              <tr key={plan.id}>
                <th scope="row" className={`${cell} font-medium`}>
                  <Link to={`/remediation/${String(plan.id)}`} className={linkClass}>
                    {`Plan ${String(plan.id)}`}
                  </Link>
                </th>
                <td className={cell}>{planStateLabels[plan.state]}</td>
                <td className={cell}>{plan.steps}</td>
                <td className={cell}>{plan.skipped}</td>
                <td className={cell}>{userLabel(plan.created_by_user_id, user)}</td>
                <td className={`${cell} whitespace-nowrap`}>{formatTime(plan.created_at)}</td>
                <td className={`${cell} whitespace-nowrap`}>{formatTime(plan.expires_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-sm text-slate-600" aria-live="polite">
        {`Showing ${String(paged.items.length)} plans${more}.`}
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
          {paged.pending ? "Loading…" : "Load more plans"}
        </button>
      )}
    </div>
  );
}

export function RemediationPage() {
  const { session } = useAuth();
  const [params] = useSearchParams();
  const raw = params.get("state");
  const state = isPlanState(raw) ? raw : undefined;
  const canPlan = session.status === "signed_in" && hasPermission(session.user, CAN_PLAN);

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Remediation
        </h1>
        <p className="mt-2 text-slate-600">
          Plans of the GitLab changes that would fix open, failing findings, built from stored
          evidence. Someone other than a plan's creator must approve it, and every step is audited.
          "Expired" includes any plan past its expiry.
        </p>
      </div>
      <DryRunNotice />
      {canPlan ? (
        <p>
          <Link to="/remediation/new" className={buttonClass}>
            Create a dry-run plan
          </Link>
        </p>
      ) : (
        <p className="text-sm text-slate-600">
          Creating plans needs the remediation planning permission, which admins and analysts have.
        </p>
      )}
      <Filters state={state} />
      <Listing state={state} />
    </section>
  );
}
