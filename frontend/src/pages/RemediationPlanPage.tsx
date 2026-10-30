import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router";

import { ApiError } from "../api/client";
import {
  CAN_APPROVE,
  CAN_PLAN,
  MAX_NOTE,
  PlanError,
  approvePlan,
  getPlan,
  rejectPlan,
  submitPlan,
  type Plan,
  type PlanStep,
} from "../api/remediation";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { DryRunNotice } from "../remediation/DryRunNotice";
import { SkippedTable } from "../remediation/SkippedTable";
import {
  actionLabels,
  describeValues,
  hasPermission,
  isCreator,
  planStateLabels,
  planStateMeaning,
  requestText,
  userLabel,
} from "../remediation/format";
import { formatTime } from "../time";

const errorClass = "rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900";
const warningClass = "rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900";
const codeClass =
  "mt-1 overflow-x-auto rounded bg-slate-900 p-3 font-mono text-xs whitespace-pre text-slate-100";

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="py-2 sm:grid sm:grid-cols-3 sm:gap-4">
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="mt-1 sm:col-span-2 sm:mt-0">{children}</dd>
    </div>
  );
}

function Lines({ lines }: { lines: string[] }) {
  return (
    <ul className="space-y-0.5">
      {lines.map((line) => (
        <li key={line} className="font-mono text-xs">
          {line}
        </li>
      ))}
    </ul>
  );
}

function Step({ step }: { step: PlanStep }) {
  const label = actionLabels[step.action] ?? step.action;
  const headingId = `step-${String(step.step)}`;
  return (
    <li>
      <article aria-labelledby={headingId} className="rounded border border-slate-200 p-4">
        <h3 id={headingId} className="font-semibold">
          {`Step ${String(step.step)}: ${label} for ${step.target.project_path}`}
        </h3>
        <dl className="mt-2 divide-y divide-slate-100 text-sm">
          <Field term="Change">
            <div className="grid gap-2 sm:grid-cols-2">
              <div>
                <p className="font-medium">Current (observed)</p>
                <Lines lines={describeValues(step.current)} />
              </div>
              <div>
                <p className="font-medium">Intended</p>
                <Lines lines={describeValues(step.intended)} />
              </div>
            </div>
          </Field>
          <Field term="Policy expects">
            <Lines lines={describeValues(step.expected)} />
          </Field>
          <Field term="Request that would be sent">
            <pre aria-label={`Request for step ${String(step.step)}`} className={codeClass}>
              {requestText(step.request)}
            </pre>
            <p className="mt-1 text-slate-600">Not sent: execution is not available.</p>
          </Field>
          <Field term="Verified by reading">
            <pre
              aria-label={`Verification read for step ${String(step.step)}`}
              className={codeClass}
            >
              {`${step.verify.method} ${step.verify.path}\nexpect ${JSON.stringify(step.verify.expect ?? {})}`}
            </pre>
          </Field>
          <Field term="Preconditions to re-check">
            <ul className="list-disc space-y-1 pl-5">
              {step.preconditions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </Field>
          <Field term="Blast radius">
            <ul className="list-disc space-y-1 pl-5">
              {step.effects.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </Field>
          <Field term="GitLab requires">{`${step.requires.role}; tier: ${step.requires.tier}`}</Field>
          <Field term="Based on">
            <ul className="space-y-1">
              <li>
                <Link to={`/findings/${String(step.finding_id)}`} className={linkClass}>
                  {`Finding ${String(step.finding_id)}`}
                </Link>
                {`, evaluation ${String(step.evaluation_id)}`}
              </li>
              <li>
                {`Policy ${step.policy.name} version ${String(step.policy.version)}, rule ${step.rule.id} (${step.rule.type}, severity ${step.rule.severity})`}
              </li>
              <li>
                {`Evidence ${String(step.observation_id)} from ${step.observation_source}, collected ${formatTime(step.observed_at)}`}
              </li>
              <li className="break-all font-mono text-xs">
                {`Evidence digest ${step.observation_digest ?? "none"}`}
              </li>
            </ul>
          </Field>
          <Field term="Idempotency key">
            <code className="break-all font-mono text-xs">{step.idempotency_key}</code>
          </Field>
        </dl>
      </article>
    </li>
  );
}

type Action = "submit" | "approve" | "reject";

const actionText: Record<Action, { open: string; heading: string; confirm: string; done: string }> =
  {
    submit: {
      open: "Submit for approval…",
      heading: "Submit this plan for approval?",
      confirm: "Confirm submission",
      done: "submitted for approval",
    },
    approve: {
      open: "Approve…",
      heading: "Approve this plan?",
      confirm: "Confirm approval",
      done: "approved",
    },
    reject: {
      open: "Reject…",
      heading: "Reject this plan?",
      confirm: "Confirm rejection",
      done: "rejected",
    },
  };

function effect(action: Action, plan: Plan): string {
  if (action === "submit") {
    return "Someone other than you must then approve or reject it before it expires.";
  }
  if (action === "approve") {
    return (
      `Your approval binds to plan digest ${plan.plan_digest}: it covers exactly the steps ` +
      "shown, and is refused if the plan's findings or evidence have changed. Approving " +
      "changes nothing in GitLab; execution is not available in this version."
    );
  }
  return "Nothing will be done with it; a new plan can be built.";
}

interface Failure {
  message: string;
  reasons: string[];
}

function Confirm({
  plan,
  action,
  csrfToken,
  onDone,
  onCancel,
}: {
  plan: Plan;
  action: Action;
  csrfToken: string;
  onDone: (updated: Plan) => void;
  onCancel: () => void;
}) {
  const { refresh } = useAuth();
  const text = actionText[action];
  const [failure, setFailure] = useState<Failure | null>(null);
  const [sending, setSending] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const alert = useRef<HTMLDivElement>(null);

  useEffect(() => {
    heading.current?.focus();
  }, []);
  useEffect(() => {
    if (failure) alert.current?.focus();
  }, [failure]);

  async function send(note: string): Promise<Plan> {
    if (action === "submit") return submitPlan(plan.id, csrfToken);
    if (action === "approve") return approvePlan(plan.id, plan.plan_digest, note, csrfToken);
    return rejectPlan(plan.id, note, csrfToken);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const note = String(new FormData(event.currentTarget).get("note") ?? "");
    if (action === "reject" && !note.trim()) {
      setFailure({ message: "Give a reason; it is recorded with the rejection.", reasons: [] });
      return;
    }
    setSending(true);
    setFailure(null);
    try {
      onDone(await send(note));
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure({
        message: apiError.message,
        reasons: apiError instanceof PlanError ? apiError.reasons : [],
      });
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-labelledby="confirm-heading"
      noValidate
      className="mt-3 space-y-3 rounded border border-slate-300 p-3"
    >
      <h3 id="confirm-heading" ref={heading} tabIndex={-1} className="font-medium">
        {text.heading}
      </h3>
      <p className="text-sm break-words">{`${effect(action, plan)} This change is audited.`}</p>
      {failure && (
        <div ref={alert} role="alert" tabIndex={-1} className={errorClass}>
          <p>{failure.message}</p>
          {failure.reasons.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5">
              {failure.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {action !== "submit" && (
        <label className="block text-sm font-medium">
          {action === "reject" ? "Reason" : "Note (optional)"}
          <textarea name="note" rows={3} maxLength={MAX_NOTE} className={fieldClass} />
        </label>
      )}
      <div className="flex gap-2">
        <button type="submit" disabled={sending} className={buttonClass}>
          {sending ? "Sending…" : text.confirm}
        </button>
        <button type="button" onClick={onCancel} className={secondaryClass}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function Actions({
  plan,
  user,
  onChanged,
}: {
  plan: Plan;
  user: CurrentUser;
  onChanged: (updated: Plan, action: Action) => void;
}) {
  const [action, setAction] = useState<Action | null>(null);
  const creator = isCreator(plan, user);
  const available: Action[] = [];
  if (plan.state === "draft" && hasPermission(user, CAN_PLAN) && creator) {
    available.push("submit");
  }
  const mayDecide = plan.state === "submitted" && hasPermission(user, CAN_APPROVE);
  if (mayDecide && !creator && plan.digest_valid) available.push("approve", "reject");
  if (mayDecide && !creator && !plan.digest_valid) available.push("reject");

  const notes: string[] = [];
  if (mayDecide && creator) {
    notes.push("You created this plan. Someone else must approve or reject it.");
  }
  if (mayDecide && !plan.digest_valid) {
    notes.push("The stored plan no longer matches its digest, so it cannot be approved.");
  }
  if (available.length === 0 && notes.length === 0) return null;

  return (
    <section aria-labelledby="actions" className="rounded border border-slate-200 bg-white p-4">
      <h2 id="actions" className="text-lg font-semibold">
        Decision
      </h2>
      {notes.map((note) => (
        <p key={note} className="mt-2 text-sm">
          {note}
        </p>
      ))}
      {action ? (
        <Confirm
          plan={plan}
          action={action}
          csrfToken={user.csrf_token}
          onDone={(updated) => {
            setAction(null);
            onChanged(updated, action);
          }}
          onCancel={() => {
            setAction(null);
          }}
        />
      ) : (
        <div className="mt-3 flex gap-2">
          {available.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => {
                setAction(item);
              }}
              className={item === "reject" ? secondaryClass : buttonClass}
            >
              {actionText[item].open}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function Detail({ initial, created }: { initial: Plan; created: boolean }) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const [changed, setChanged] = useState<{ plan: Plan; action: Action }>();
  const status = useRef<HTMLParagraphElement>(null);
  const plan = changed?.plan ?? initial;
  const doc = plan.plan;
  const pending = plan.state === "draft" || plan.state === "submitted";

  useEffect(() => {
    if (changed || created) status.current?.focus();
  }, [changed, created]);

  let message: string | null = null;
  if (changed) message = `Plan ${String(plan.id)} ${actionText[changed.action].done}.`;
  else if (created) {
    message = `Plan ${String(plan.id)} created as a draft. Review it, then submit it for approval.`;
  }

  return (
    <>
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="text-sm font-medium">
          {`${message} Nothing was changed in GitLab.`}
        </p>
      )}
      {!plan.digest_valid && (
        <p role="alert" className={errorClass}>
          The stored plan no longer matches its digest: it was altered after it was created. Do not
          rely on it; it cannot be approved.
        </p>
      )}
      {pending && plan.stale_reasons.length > 0 && (
        <div role="alert" className={warningClass}>
          <p className="font-medium">This plan is stale and would be refused for approval:</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {plan.stale_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          <p className="mt-1">Build a new plan from the current findings instead.</p>
        </div>
      )}
      <section aria-labelledby="summary" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="summary" className="text-lg font-semibold">
          Summary
        </h2>
        <dl className="mt-2 divide-y divide-slate-100 text-sm">
          <Field term="State">{`${planStateLabels[plan.state]}: ${planStateMeaning[plan.state]}`}</Field>
          <Field term="Steps">
            {`${String(plan.steps)} to change, ${String(plan.skipped)} skipped`}
          </Field>
          <Field term="Created">
            {`${formatTime(plan.created_at)} by ${userLabel(plan.created_by_user_id, user)}`}
          </Field>
          <Field term="Expires">{formatTime(plan.expires_at)}</Field>
          {plan.submitted_at && <Field term="Submitted">{formatTime(plan.submitted_at)}</Field>}
          {plan.decided_at && (
            <Field term={plan.state === "rejected" ? "Rejected" : "Decided"}>
              {`${formatTime(plan.decided_at)} by ${userLabel(plan.decided_by_user_id, user)}`}
              {plan.decision_note && (
                <span className="block whitespace-pre-wrap text-slate-700">
                  {plan.decision_note}
                </span>
              )}
            </Field>
          )}
          <Field term="Plan digest">
            <code className="break-all font-mono text-xs">{plan.plan_digest}</code>
            <span className="block text-slate-600">
              {plan.digest_valid
                ? "Recomputed by the API on this read: it matches."
                : "Recomputed by the API on this read: it does not match."}
            </span>
          </Field>
          {plan.approved_digest && (
            <Field term="Approved digest">
              <code className="break-all font-mono text-xs">{plan.approved_digest}</code>
            </Field>
          )}
          <Field term="Instance">{doc.instance}</Field>
          <Field term="Execution">{doc.execution}</Field>
        </dl>
        <h3 className="mt-4 font-medium">Changes at a glance</h3>
        <pre aria-label="Plan summary" className={codeClass}>
          {plan.summary.join("\n")}
        </pre>
      </section>
      {user && (
        <Actions
          plan={plan}
          user={user}
          onChanged={(updated, action) => {
            setChanged({ plan: updated, action });
          }}
        />
      )}
      <section aria-labelledby="steps" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="steps" className="text-lg font-semibold">
          Steps
        </h2>
        <ol className="mt-3 space-y-4">
          {doc.steps.map((step) => (
            <Step key={step.step} step={step} />
          ))}
        </ol>
      </section>
      <section aria-labelledby="skipped" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="skipped" className="text-lg font-semibold">
          Skipped findings
        </h2>
        {doc.skipped.length === 0 ? (
          <p className="mt-2 text-sm">Every finding in scope has a step.</p>
        ) : (
          <SkippedTable skipped={doc.skipped} caption="Skipped findings" />
        )}
      </section>
      <section aria-labelledby="excluded" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="excluded" className="text-lg font-semibold">
          Never planned
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Changes remediation does not make, whatever the findings say.
        </p>
        <dl className="mt-2 divide-y divide-slate-100 text-sm">
          {Object.entries(doc.excluded_actions).map(([action, reason]) => (
            <Field key={action} term={action}>
              {reason}
            </Field>
          ))}
        </dl>
      </section>
    </>
  );
}

export function RemediationPlanPage() {
  const { planId = "" } = useParams();
  const location = useLocation();
  const created = (location.state as { created?: boolean } | null)?.created === true;
  const id = Number(planId);
  const valid = Number.isSafeInteger(id) && id > 0 && String(id) === planId;
  const loaded = useLoad(`plan:${planId}`, (signal) =>
    valid ? getPlan(id, signal) : Promise.reject(new ApiError(404, "No such plan")),
  );

  let body: ReactNode;
  if (loaded.error?.status === 404) {
    body = <p className="text-slate-600">{`There is no remediation plan with ID ${planId}.`}</p>;
  } else if (loaded.error) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (!loaded.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading plan…
      </p>
    );
  } else {
    body = <Detail key={loaded.data.id} initial={loaded.data} created={created} />;
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <p className="text-sm">
          <Link to="/remediation" className={linkClass}>
            All remediation plans
          </Link>
        </p>
        <h1 id="page-title" className="mt-2 text-2xl font-semibold">
          {valid ? `Remediation plan ${planId}` : "Remediation plan"}
        </h1>
      </div>
      <DryRunNotice />
      {body}
    </section>
  );
}
