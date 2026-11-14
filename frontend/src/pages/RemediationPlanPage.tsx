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
import { DecisionBar } from "../ui/DecisionBar";
import { useDeviceClass } from "../ui/device";
import { Dialog } from "../ui/Dialog";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

const codeClass = "mb-0 small";

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="row g-2 py-2 border-bottom">
      <dt className="col-md-4 fw-medium text-secondary">{term}</dt>
      <dd className="col-md-8 mb-0" style={{ minWidth: 0 }}>
        {children}
      </dd>
    </div>
  );
}

function Lines({ lines }: { lines: string[] }) {
  return (
    <ul className="list-unstyled mb-0">
      {lines.map((line) => (
        <li key={line} className="font-monospace small text-break">
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
      <article aria-labelledby={headingId} className="card card-sm">
        <div className="card-header">
          <h3 id={headingId} className="card-title">
            {`Step ${String(step.step)}: ${label} for ${step.target.project_path}`}
          </h3>
        </div>
        <dl className="card-body mb-0 py-0">
          <Field term="Change">
            <div className="row g-2">
              <div className="col-sm-6">
                <p className="fw-medium mb-1">Current (observed)</p>
                <Lines lines={describeValues(step.current)} />
              </div>
              <div className="col-sm-6">
                <p className="fw-medium mb-1">Intended</p>
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
            <p className="mt-1 mb-0 text-secondary">Not sent: execution is not available.</p>
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
            <ul className="mb-0 ps-3">
              {step.preconditions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </Field>
          <Field term="Blast radius">
            <ul className="mb-0 ps-3">
              {step.effects.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </Field>
          <Field term="GitLab requires">{`${step.requires.role}; tier: ${step.requires.tier}`}</Field>
          <Field term="Based on">
            <ul className="list-unstyled mb-0">
              <li>
                <Link to={`/findings/${String(step.finding_id)}`}>
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
              <li className="text-break font-monospace small">
                {`Evidence digest ${step.observation_digest ?? "none"}`}
              </li>
            </ul>
          </Field>
          <Field term="Idempotency key">
            <code className="text-break bg-transparent p-0">{step.idempotency_key}</code>
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
  const alert = useRef<HTMLDivElement>(null);

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
    <form onSubmit={(event) => void submit(event)} aria-label={text.heading} noValidate>
      <p className="text-break">{`${effect(action, plan)} This change is audited.`}</p>
      {failure && (
        <div ref={alert} role="alert" tabIndex={-1} className="alert alert-danger text-body">
          <p className="mb-0">{failure.message}</p>
          {failure.reasons.length > 0 && (
            <ul className="mt-2 mb-0 ps-3">
              {failure.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {action !== "submit" && (
        <label className="form-label w-100 mb-3">
          {action === "reject" ? "Reason" : "Note (optional)"}
          <textarea name="note" rows={3} maxLength={MAX_NOTE} className="form-control mt-1" />
        </label>
      )}
      <div className="btn-list justify-content-end">
        <button type="button" onClick={onCancel} className="btn">
          Cancel
        </button>
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Sending…" : text.confirm}
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
  const device = useDeviceClass();
  const creator = isCreator(plan, user);
  const ownDecision = creator && user.development_mode !== true;
  const available: Action[] = [];
  if (plan.state === "draft" && hasPermission(user, CAN_PLAN) && creator) {
    available.push("submit");
  }
  const mayDecide = plan.state === "submitted" && hasPermission(user, CAN_APPROVE);
  if (mayDecide && !ownDecision && plan.digest_valid) available.push("approve", "reject");
  if (mayDecide && !ownDecision && !plan.digest_valid) available.push("reject");

  const notes: string[] = [];
  if (mayDecide && ownDecision) {
    notes.push("You created this plan. Someone else must approve or reject it.");
  }
  if (mayDecide && !plan.digest_valid) {
    notes.push("The stored plan no longer matches its digest, so it cannot be approved.");
  }
  if (available.length === 0 && notes.length === 0) return null;
  // Phones offer decisions only.
  const shown = device === "phone" ? available.filter((item) => item !== "submit") : available;

  return (
    <>
      <DecisionBar
        summary={
          notes.length > 0
            ? notes.join(" ")
            : "Approving binds to the plan digest shown; nothing is changed in GitLab."
        }
      >
        {shown.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => {
              setAction(item);
            }}
            className={item === "reject" ? "btn" : "btn btn-primary"}
          >
            {actionText[item].open}
          </button>
        ))}
      </DecisionBar>
      <Dialog
        open={action !== null}
        title={action ? actionText[action].heading : ""}
        onClose={() => {
          setAction(null);
        }}
      >
        {action && (
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
        )}
      </Dialog>
    </>
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
        <p ref={status} role="status" tabIndex={-1} className="alert alert-success text-body">
          {`${message} Nothing was changed in GitLab.`}
        </p>
      )}
      {!plan.digest_valid && (
        <p role="alert" className="alert alert-danger text-body">
          The stored plan no longer matches its digest: it was altered after it was created. Do not
          rely on it; it cannot be approved.
        </p>
      )}
      {pending && plan.stale_reasons.length > 0 && (
        <div role="alert" className="alert alert-warning text-body">
          <p className="fw-medium mb-1">This plan is stale and would be refused for approval:</p>
          <ul className="mb-1 ps-3">
            {plan.stale_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          <p className="mb-0">Build a new plan from the current findings instead.</p>
        </div>
      )}
      <section aria-labelledby="summary" className="card mb-3">
        <div className="card-header">
          <h2 id="summary" className="card-title">
            Summary
          </h2>
        </div>
        <div className="card-body">
          <dl className="mb-0">
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
                  <span className="d-block text-secondary" style={{ whiteSpace: "pre-wrap" }}>
                    {plan.decision_note}
                  </span>
                )}
              </Field>
            )}
            <Field term="Plan digest">
              <code className="text-break bg-transparent p-0">{plan.plan_digest}</code>
              <span className="d-block text-secondary">
                {plan.digest_valid
                  ? "Recomputed by the API on this read: it matches."
                  : "Recomputed by the API on this read: it does not match."}
              </span>
            </Field>
            {plan.approved_digest && (
              <Field term="Approved digest">
                <code className="text-break bg-transparent p-0">{plan.approved_digest}</code>
              </Field>
            )}
            <Field term="Instance">{doc.instance}</Field>
            <Field term="Execution">{doc.execution}</Field>
          </dl>
          <h3 className="mt-3 h4">Changes at a glance</h3>
          <pre aria-label="Plan summary" className={codeClass}>
            {plan.summary.join("\n")}
          </pre>
        </div>
      </section>
      <section aria-labelledby="steps" className="mb-3">
        <h2 id="steps" className="h3">
          Steps
        </h2>
        <ol className="list-unstyled d-grid gap-3 mb-0">
          {doc.steps.map((step) => (
            <Step key={step.step} step={step} />
          ))}
        </ol>
      </section>
      <section aria-labelledby="skipped" className="card mb-3">
        <div className="card-header">
          <h2 id="skipped" className="card-title">
            Skipped findings
          </h2>
        </div>
        {doc.skipped.length === 0 ? (
          <p className="card-body mb-0">Every finding in scope has a step.</p>
        ) : (
          <SkippedTable skipped={doc.skipped} caption="Skipped findings" />
        )}
      </section>
      <section aria-labelledby="excluded" className="card mb-3">
        <div className="card-header d-block">
          <h2 id="excluded" className="card-title">
            Never planned
          </h2>
          <p className="text-secondary small mb-0 mt-1">
            Changes remediation does not make, whatever the findings say.
          </p>
        </div>
        <dl className="card-body mb-0 py-0">
          {Object.entries(doc.excluded_actions).map(([action, reason]) => (
            <Field key={action} term={action}>
              {reason}
            </Field>
          ))}
        </dl>
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
    body = <p className="text-secondary">{`There is no remediation plan with ID ${planId}.`}</p>;
  } else if (loaded.error) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (!loaded.data) {
    body = <Loading label="Loading plan…" />;
  } else {
    body = <Detail key={loaded.data.id} initial={loaded.data} created={created} />;
  }

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to="/remediation">All remediation plans</Link>}
        title={valid ? `Remediation plan ${planId}` : "Remediation plan"}
      />
      <DryRunNotice />
      {body}
    </section>
  );
}
