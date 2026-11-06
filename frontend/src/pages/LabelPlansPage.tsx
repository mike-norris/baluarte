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
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
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
import { formatTime } from "../time";

const cell = "px-3 py-2";
const headers = ["Plan", "State", "Scope", "Steps", "By hand", "Created by", "Created", "Expires"];

/** Build a plan: Baluarte reads the scope's labels again first, so the plan starts current. */
function CreatePlan() {
  const { session, refresh } = useAuth();
  const { chosen } = useAsOfState();
  const navigate = useNavigate();
  const id = useId();
  const [groups, setGroups] = useState("");
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const user = session.status === "signed_in" ? session.user : null;
  if (!user || !hasPermission(user, CAN_PLAN)) {
    return (
      <p className="text-sm text-slate-600">
        Creating plans needs the remediation planning permission, which admins and analysts have.
      </p>
    );
  }
  if (chosen !== null) return <PastReadOnly />;
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
      className="space-y-3 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id={`${id}-title`} className="text-lg font-semibold">
        Create a plan
      </h2>
      <p className="text-sm text-slate-600">
        Baluarte reads the labels of the groups and projects in scope from GitLab now (read only),
        then lists the creates, renames and updates that would fix the drift. Duplicates, misplaced
        labels and labels not in the taxonomy are listed for you to fix by hand; nothing is ever
        deleted.
      </p>
      <label className="block text-sm font-medium" htmlFor={`${id}-groups`}>
        Top-level groups (comma-separated; leave empty for the taxonomy's whole scope)
        <input
          id={`${id}-groups`}
          value={groups}
          onChange={(event) => {
            setGroups(event.target.value);
          }}
          className={fieldClass}
        />
      </label>
      {failure && (
        <p role="alert" className="text-sm text-red-900">
          {failure}
        </p>
      )}
      <button type="submit" disabled={sending} className={buttonClass}>
        {sending ? "Reading labels and building the plan…" : "Create a plan"}
      </button>
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
      className="flex flex-wrap items-end gap-3 rounded border border-slate-200 bg-white p-4"
    >
      <label className="text-sm font-medium">
        State
        <select name="state" defaultValue={state ?? ""} className={fieldClass}>
          <option value="">Any</option>
          {labelPlanStates.map((item) => (
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
        <Link to={LABEL_PLANS} className={secondaryClass}>
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

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (!first.data) {
    return (
      <p role="status" className="text-slate-600">
        Loading label plans…
      </p>
    );
  }
  if (paged.items.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {state ? "No plans are in this state." : "No label plans have been created."}
      </p>
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Label plans</caption>
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
                  <Link to={labelPlanHref(plan.id)} className={linkClass}>
                    {`Plan ${String(plan.id)}`}
                  </Link>
                </th>
                <td className={cell}>{planStateLabels[plan.state]}</td>
                <td className={cell}>
                  {plan.scope.groups.length > 0 ? plan.scope.groups.join(", ") : "Whole taxonomy"}
                </td>
                <td className={cell}>{plan.steps}</td>
                <td className={cell}>{plan.manual}</td>
                <td className={cell}>{personText(plan.created_by)}</td>
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

export function LabelPlansPage() {
  const [params] = useSearchParams();
  const raw = params.get("state");
  const state = isPlanState(raw) ? raw : undefined;
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <p className="text-sm">
          <Link to={LABELS} className={linkClass}>
            Labels
          </Link>
        </p>
        <h1 id="page-title" className="text-2xl font-semibold">
          Label plans
        </h1>
        <p className="mt-2 max-w-3xl text-slate-600">
          Plans of the label changes that would bring groups and projects in line with the taxonomy.
          A plan is applied only after approval, with a separate write token, one step at a time,
          each read back to verify it. Labels are created, renamed or updated, never deleted.
        </p>
      </div>
      <CreatePlan />
      <Filters state={state} />
      <Listing state={state} />
    </section>
  );
}
