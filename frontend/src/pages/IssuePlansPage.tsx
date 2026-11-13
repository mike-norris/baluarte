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
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { isPlanState, parseAliases, planStateLabels } from "../labels/format";
import { personText } from "../policies/format";
import { hasPermission } from "../remediation/format";
import { formatTime } from "../time";

const cell = "px-3 py-2";
const headers = ["Plan", "State", "Scope", "Steps", "By hand", "Created by", "Created", "Expires"];

/** What a plan would change now, from the closed issues as last collected. */
function Preview() {
  const loaded = useLoad("issue-plans:preview", (signal) => getIssuePreview(null, signal));
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) {
    return (
      <p role="status" className="text-slate-600">
        Loading closed issues…
      </p>
    );
  }
  const preview = loaded.data;
  const rules = preview.rules;
  return (
    <section
      aria-labelledby="rules-title"
      className="space-y-3 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="rules-title" className="text-lg font-semibold">
        Rules
      </h2>
      <ul className="list-disc space-y-1 pl-5 text-sm">
        <li>
          {`A closed issue with commits (a merge request, or a commit mentioning it) and no time spent gets the label "${rules.missing_time_label}".`}
        </li>
        <li>{`Once time is logged on it, the label is removed.`}</li>
        <li>
          {`A closed issue with time spent and no estimate gets the time spent, rounded up to ${durationText(rules.estimate_round_minutes * 60)}, as its estimate. An estimate is never changed.`}
        </li>
        <li>{`Issue types covered: ${rules.issue_types.join(", ")}.`}</li>
      </ul>
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        {(
          [
            ["Closed issues read", preview.closed_issues],
            ["Labels to add", preview.add_label],
            ["Labels to remove", preview.remove_label],
            ["Estimates to set", preview.set_estimate],
          ] as const
        ).map(([term, value]) => (
          <div key={term} className="rounded border border-slate-100 p-2">
            <dt className="text-slate-600">{term}</dt>
            <dd className="text-xl font-semibold">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-sm text-slate-600">
        {preview.last_read_at
          ? `Closed issues last read ${formatTime(preview.last_read_at)}. `
          : "Closed issues have not been read yet; they are read with each time collection. "}
        {preview.commits_unknown > 0 &&
          `${String(preview.commits_unknown)} issues without time are waiting for their commits to be checked. `}
        {preview.writes_enabled
          ? "Issue writes are allowed for this deployment."
          : "Issue writes are turned off for this deployment (BALUARTE_ISSUE_WRITES), so plans can be built and reviewed but not applied."}
      </p>
    </section>
  );
}

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
      className="space-y-3 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id={`${id}-title`} className="text-lg font-semibold">
        Create a plan
      </h2>
      <p className="text-sm text-slate-600">
        Baluarte lists the changes from the closed issues as last read, and checks with GitLab (read
        only) which projects define the label. Nothing changes in GitLab until the plan is approved
        and applied.
      </p>
      <label className="block text-sm font-medium" htmlFor={`${id}-groups`}>
        Groups (comma-separated; leave empty for every monitored project)
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
        {sending ? "Building the plan…" : "Create a plan"}
      </button>
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
        <Link to={ISSUE_PLANS} className={secondaryClass}>
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

  if (first.error) return <ErrorMessage error={first.error} retry={first.retry} />;
  if (!first.data) {
    return (
      <p role="status" className="text-slate-600">
        Loading issue plans…
      </p>
    );
  }
  if (paged.items.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {state ? "No plans are in this state." : "No issue plans have been created."}
      </p>
    );
  }
  const more = paged.next !== null ? " (more available)" : "";
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Issue plans</caption>
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
                  <Link to={issuePlanHref(plan.id)} className={linkClass}>
                    {`Plan ${String(plan.id)}`}
                  </Link>
                </th>
                <td className={cell}>{planStateLabels[plan.state]}</td>
                <td className={cell}>
                  {plan.scope.groups.length > 0 ? plan.scope.groups.join(", ") : "All projects"}
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

export function IssuePlansPage() {
  const [params] = useSearchParams();
  const raw = params.get("state");
  const state = isPlanState(raw) ? raw : undefined;
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Closed issue time
        </h1>
        <p className="mt-2 max-w-3xl text-slate-600">
          Closed issues should account for their time. A plan flags issues closed with commits but
          no time spent, and gives issues without an estimate one from their time spent. It is
          applied only after approval, with the separate write token, one issue at a time, each read
          back to verify it.
        </p>
      </div>
      <Preview />
      <CreatePlan />
      <Filters state={state} />
      <Listing state={state} />
    </section>
  );
}
