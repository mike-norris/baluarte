import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router";

import { ApiError } from "../api/client";
import {
  approveIssuePlan,
  getIssuePlan,
  issueApplyBlocks,
  issuePlanAction,
  ISSUE_PLANS,
  issueStepStatusLabels,
  rejectIssuePlan,
  stepChange,
  type IssueManualItem,
  type IssuePlan,
  type IssuePlanStep,
} from "../api/issuePlans";
import { CAN_APPROVE, CAN_PLAN, MAX_NOTE } from "../api/labels";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { leavesOwnToOthers, type CurrentUser } from "../auth/session";
import { useAsOfState } from "../asof/context";
import { pastReadOnly } from "../asof/format";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { planStateLabels, planStateMeaning } from "../labels/format";
import { personText } from "../policies/format";
import { hasPermission } from "../remediation/format";
import { formatTime } from "../time";

/** How often a plan being applied is read again. */
export const APPLY_POLL_MS = 3000;

const cell = "px-3 py-2 align-top";
const errorClass = "rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900";

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="py-2 sm:grid sm:grid-cols-3 sm:gap-4">
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="mt-1 sm:col-span-2 sm:mt-0">{children}</dd>
    </div>
  );
}

const statusClass: Record<IssuePlanStep["status"], string> = {
  pending: "bg-slate-100 text-slate-800",
  skipped: "bg-sky-100 text-sky-900",
  stale: "bg-slate-200 text-slate-900",
  applied: "bg-emerald-100 text-emerald-900",
  failed: "bg-red-100 text-red-900",
  not_run: "bg-amber-100 text-amber-900",
};

function verifiedText(step: IssuePlanStep): string {
  if (step.verified === null) return step.status === "pending" ? "Not yet" : "Not read back";
  return step.verified ? "Read back: as intended" : "Read back: not as intended";
}

function Steps({ steps }: { steps: IssuePlanStep[] }) {
  if (steps.length === 0) {
    return <p className="mt-2 text-sm">No changes: everything left needs fixing by hand.</p>;
  }
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Plan steps, in the order they are applied</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            {["Step", "Issue", "Change", "Result", "Verified"].map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {steps.map((step) => (
            <tr key={step.step}>
              <th scope="row" className={`${cell} font-medium`}>
                {step.step}
              </th>
              <td className={cell}>
                <IssueLink step={step} />
                {step.issue.title && (
                  <span className="block text-xs text-slate-600">{step.issue.title}</span>
                )}
              </td>
              <td className={cell}>{stepChange(step)}</td>
              <td className={cell}>
                <span className={`rounded px-1.5 py-0.5 ${statusClass[step.status]}`}>
                  {issueStepStatusLabels[step.status]}
                </span>
                {step.request_status !== null && (
                  <span className="block text-xs text-slate-600">
                    {`GitLab answered ${String(step.request_status)}`}
                  </span>
                )}
                {step.message && <span className="block text-xs">{step.message}</span>}
              </td>
              <td className={cell}>{verifiedText(step)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function IssueLink({ step }: { step: IssuePlanStep }) {
  const text = `${step.project.path}#${String(step.issue.iid)}`;
  if (!step.issue.web_url) return <span>{text}</span>;
  return (
    <a href={step.issue.web_url} target="_blank" rel="noreferrer" className={linkClass}>
      {text}
    </a>
  );
}

function Manual({ items }: { items: IssueManualItem[] }) {
  if (items.length === 0) return <p className="mt-2 text-sm">Nothing needs fixing by hand.</p>;
  return (
    <ul className="mt-2 space-y-2 text-sm">
      {items.map((item) => (
        <li key={`${item.reason}:${String(item.project.gitlab_id)}`}>
          <span className="font-medium">{item.project.path}</span>
          {`: ${String(item.issues.length)} issues (${item.issues
            .slice(0, 20)
            .map((iid) => `#${String(iid)}`)
            .join(", ")}${item.issues.length > 20 ? ", …" : ""}). ${item.message}`}
        </li>
      ))}
    </ul>
  );
}

type Action = "submit" | "approve" | "reject" | "apply";

const done: Record<Action, string> = {
  submit: "submitted for approval",
  approve: "approved",
  reject: "rejected",
  apply: "is being applied",
};

function Actions({
  plan,
  user,
  onChanged,
}: {
  plan: IssuePlan;
  user: CurrentUser;
  onChanged: (plan: IssuePlan, action: Action) => void;
}) {
  const { refresh } = useAuth();
  const { chosen } = useAsOfState();
  const id = useId();
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<Action | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const creator = plan.created_by.user_id === user.user_id;
  const ownDecision = leavesOwnToOthers(user, plan.created_by.user_id);
  const canPlan = hasPermission(user, CAN_PLAN);
  const canApprove = hasPermission(user, CAN_APPROVE);
  const past = chosen !== null;

  async function run(action: Action) {
    if (action === "reject" && !note.trim()) {
      setFailure("Explain why in the note before rejecting.");
      return;
    }
    setSending(action);
    setFailure(null);
    try {
      let updated: IssuePlan;
      if (action === "approve") {
        updated = await approveIssuePlan(plan.id, plan.plan_digest, note, user.csrf_token);
      } else if (action === "reject") {
        updated = await rejectIssuePlan(plan.id, note, user.csrf_token);
      } else updated = await issuePlanAction(plan.id, action, user.csrf_token);
      onChanged(updated, action);
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(null);
    }
  }

  const blocks = issueApplyBlocks(plan.can_apply.reasons);
  if (past) blocks.push(pastReadOnly);
  const applyAllowed = plan.can_apply.allowed && !past;
  const showSubmit = plan.state === "draft" && canPlan && creator;
  const mayDecide = plan.state === "submitted" && canApprove;
  const showApply = canPlan && (plan.state === "approved" || plan.state === "submitted");

  if (!showSubmit && !mayDecide && !showApply) return null;
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="rounded border border-slate-200 bg-white p-4"
    >
      <h2 id={`${id}-title`} className="text-lg font-semibold">
        Actions
      </h2>
      {failure && (
        <p role="alert" className="mt-2 text-sm text-red-900">
          {failure}
        </p>
      )}
      {showSubmit && (
        <div className="mt-3 space-y-2">
          <p className="text-sm">
            {plan.review_required
              ? "Submitting asks someone other than you to approve it."
              : "Review is off: submitting approves it at once, recorded as self-approved."}
          </p>
          <button
            type="button"
            disabled={sending !== null || past}
            onClick={() => void run("submit")}
            className={buttonClass}
          >
            {sending === "submit" ? "Submitting…" : "Submit for approval"}
          </button>
        </div>
      )}
      {mayDecide &&
        (ownDecision ? (
          <p className="mt-2 text-sm">
            You created this plan. Someone else must approve or reject it.
          </p>
        ) : !plan.digest_valid ? (
          <p className="mt-2 text-sm">
            The stored plan no longer matches its digest, so it cannot be approved.
          </p>
        ) : (
          <div className="mt-3 space-y-2">
            <label htmlFor={`${id}-note`} className="text-sm font-medium">
              Note (required to reject)
            </label>
            <textarea
              id={`${id}-note`}
              value={note}
              maxLength={MAX_NOTE}
              rows={2}
              onChange={(event) => {
                setNote(event.target.value);
              }}
              className={fieldClass}
            />
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={sending !== null || past}
                onClick={() => void run("approve")}
                className={buttonClass}
              >
                {sending === "approve" ? "Approving…" : "Approve this digest"}
              </button>
              <button
                type="button"
                disabled={sending !== null || past}
                onClick={() => void run("reject")}
                className={secondaryClass}
              >
                {sending === "reject" ? "Rejecting…" : "Reject"}
              </button>
            </div>
          </div>
        ))}
      {showApply && (
        <div className="mt-4 space-y-2">
          <button
            type="button"
            disabled={!applyAllowed || sending !== null}
            aria-describedby={applyAllowed ? undefined : `${id}-why-not`}
            onClick={() => void run("apply")}
            className={buttonClass}
          >
            {sending === "apply" ? "Starting…" : "Apply in GitLab"}
          </button>
          {!applyAllowed && (
            <div id={`${id}-why-not`} className="text-sm">
              <p className="font-medium">It can't be applied now:</p>
              <ul className="list-disc space-y-1 pl-5">
                {blocks.map((block) => (
                  <li key={block}>{block}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function Detail({
  initial,
  created,
  reload,
}: {
  initial: IssuePlan;
  created: boolean;
  reload: () => void;
}) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const [changed, setChanged] = useState<{ plan: IssuePlan; action: Action; base: IssuePlan }>();
  const status = useRef<HTMLParagraphElement>(null);
  // An action's response is shown until the next read of the plan arrives.
  const plan = changed && changed.base === initial ? changed.plan : initial;
  const applying = plan.state === "applying";

  useEffect(() => {
    if (changed || created) status.current?.focus();
  }, [changed, created]);

  useEffect(() => {
    if (!applying) return;
    const timer = window.setInterval(reload, APPLY_POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [applying, reload]);

  let message: string | null = null;
  if (changed) message = `Plan ${String(plan.id)} ${done[changed.action]}.`;
  else if (created) {
    message = `Plan ${String(plan.id)} created as a draft. Review it, then submit it.`;
  }
  const applied = plan.progress.applied ?? 0;
  const skipped = plan.progress.skipped ?? 0;
  const stale = plan.progress.stale ?? 0;

  return (
    <>
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="text-sm font-medium">
          {message}
        </p>
      )}
      {!plan.digest_valid && (
        <p role="alert" className={errorClass}>
          The stored plan no longer matches its digest: it was altered after it was created. Do not
          rely on it; it cannot be approved or applied.
        </p>
      )}
      {plan.state === "failed" && (
        <p role="alert" className={errorClass}>
          {`Stopped at step ${String(plan.failed_step ?? "?")}: ${plan.failure ?? "an unexpected result"}. Later steps were not run, and no write was retried. Create a new plan.`}
        </p>
      )}
      <section aria-labelledby="summary" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="summary" className="text-lg font-semibold">
          Summary
        </h2>
        <dl className="mt-2 divide-y divide-slate-100 text-sm">
          <Field term="State">{`${planStateLabels[plan.state]}: ${planStateMeaning[plan.state]}`}</Field>
          <Field term="Scope">
            {plan.scope.groups.length > 0
              ? plan.scope.groups.join(", ")
              : "Every monitored project"}
          </Field>
          <Field term="Rules">
            {`Label "${plan.rules.missing_time_label}"; estimates rounded up to ${String(plan.rules.estimate_round_minutes)} minutes; ${plan.rules.issue_types.join(", ")}`}
          </Field>
          <Field term="Steps">
            {`${String(plan.steps)} to apply, ${String(plan.manual)} projects with issues to fix by hand`}
            {(applying || plan.state === "applied" || plan.state === "failed") && (
              <span className="block text-slate-600" aria-live="polite">
                {`${String(applied)} applied, ${String(skipped)} already in place, ${String(stale)} changed since the plan.`}
              </span>
            )}
          </Field>
          <Field term="Created">
            {`${formatTime(plan.created_at)} by ${personText(plan.created_by)}`}
          </Field>
          <Field term="Expires">{formatTime(plan.expires_at)}</Field>
          {plan.submitted_at && <Field term="Submitted">{formatTime(plan.submitted_at)}</Field>}
          {plan.decided_at && (
            <Field term={plan.state === "rejected" ? "Rejected" : "Decided"}>
              {plan.self_approved
                ? `${formatTime(plan.decided_at)}: review was off, so it was self-approved`
                : `${formatTime(plan.decided_at)} by ${personText(plan.decided_by)}`}
              {plan.decision_note && (
                <span className="block whitespace-pre-wrap text-slate-700">
                  {plan.decision_note}
                </span>
              )}
            </Field>
          )}
          {plan.apply_started_at && (
            <Field term="Applying since">{formatTime(plan.apply_started_at)}</Field>
          )}
          {plan.finished_at && <Field term="Finished">{formatTime(plan.finished_at)}</Field>}
          <Field term="Plan digest">
            <code className="break-all font-mono text-xs">{plan.plan_digest}</code>
            <span className="block text-slate-600">
              {plan.digest_valid
                ? "Recomputed by the API on this read: it matches."
                : "Recomputed by the API on this read: it does not match."}
            </span>
          </Field>
        </dl>
      </section>
      {user && (
        <Actions
          plan={plan}
          user={user}
          onChanged={(updated, action) => {
            setChanged({ plan: updated, action, base: initial });
          }}
        />
      )}
      <section aria-labelledby="steps" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="steps" className="text-lg font-semibold">
          Steps
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Applied in order. Each issue is read first: a change already in place is skipped, and an
          issue that changed since the plan is left alone. Each write is read back to verify it; a
          refused write or a mismatch stops the plan.
        </p>
        <Steps steps={plan.step_items} />
      </section>
      <section aria-labelledby="manual" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="manual" className="text-lg font-semibold">
          To fix by hand
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Issues a plan cannot change: the project does not define the label (Baluarte never creates
          one), or their commits could not be checked yet.
        </p>
        <Manual items={plan.manual_items} />
      </section>
    </>
  );
}

export function IssuePlanPage() {
  const { planId = "" } = useParams();
  const location = useLocation();
  const created = (location.state as { created?: boolean } | null)?.created === true;
  const id = Number(planId);
  const valid = Number.isSafeInteger(id) && id > 0 && String(id) === planId;
  const loaded = useLoad(`issue-plans:plan:${planId}`, (signal) =>
    valid ? getIssuePlan(id, signal) : Promise.reject(new ApiError(404, "No such plan")),
  );
  const [shown, setShown] = useState<IssuePlan | undefined>(undefined);
  const data = loaded.data ?? shown;
  if (loaded.data && loaded.data !== shown) setShown(loaded.data);

  let body: ReactNode;
  if (loaded.error?.status === 404) {
    body = <p className="text-slate-600">{`There is no issue plan with ID ${planId}.`}</p>;
  } else if (loaded.error && !data) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (!data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading plan…
      </p>
    );
  } else {
    body = <Detail key={data.id} initial={data} created={created} reload={loaded.retry} />;
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <p className="text-sm">
          <Link to={ISSUE_PLANS} className={linkClass}>
            Closed issue time
          </Link>
        </p>
        <h1 id="page-title" className="mt-2 text-2xl font-semibold">
          {valid ? `Issue plan ${planId}` : "Issue plan"}
        </h1>
      </div>
      {body}
    </section>
  );
}
