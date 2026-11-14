import { useEffect, useRef, useState, type ReactNode } from "react";
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
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { planStateLabels, planStateMeaning } from "../labels/format";
import { personText } from "../policies/format";
import { hasPermission } from "../remediation/format";
import { planLifecycle, planStateTone } from "../plans/format";
import { PlanDecision, type PlanAction } from "../plans/PlanDecision";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { DigestDisplay } from "../ui/DigestDisplay";
import { KeyValue, type KeyValueItem } from "../ui/KeyValue";
import { Lifecycle } from "../ui/Lifecycle";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

/** How often a plan being applied is read again. */
export const APPLY_POLL_MS = 3000;

const stepTone: Record<IssuePlanStep["status"], Tone> = {
  pending: "secondary",
  skipped: "info",
  stale: "secondary",
  applied: "success",
  failed: "danger",
  not_run: "warning",
};

function verifiedText(step: IssuePlanStep): string {
  if (step.verified === null) return step.status === "pending" ? "Not yet" : "Not read back";
  return step.verified ? "Read back: as intended" : "Read back: not as intended";
}

function IssueLink({ step }: { step: IssuePlanStep }) {
  const text = `${step.project.path}#${String(step.issue.iid)}`;
  if (!step.issue.web_url) return <span>{text}</span>;
  return (
    <a href={step.issue.web_url} target="_blank" rel="noreferrer">
      {text}
    </a>
  );
}

const stepColumns: readonly Column<IssuePlanStep>[] = [
  { key: "step", header: "Step", render: (step) => <span className="fw-medium">{step.step}</span> },
  {
    key: "issue",
    header: "Issue",
    render: (step) => (
      <div>
        <IssueLink step={step} />
        {step.issue.title && (
          <span className="d-block small text-secondary">{step.issue.title}</span>
        )}
      </div>
    ),
  },
  { key: "change", header: "Change", render: stepChange },
  {
    key: "result",
    header: "Result",
    render: (step) => (
      <div>
        <Badge tone={stepTone[step.status]}>{issueStepStatusLabels[step.status]}</Badge>
        {step.request_status !== null && (
          <span className="d-block small text-secondary">
            {`GitLab answered ${String(step.request_status)}`}
          </span>
        )}
        {step.message && <span className="d-block small">{step.message}</span>}
      </div>
    ),
  },
  { key: "verified", header: "Verified", render: verifiedText, priority: 2 },
];

function Manual({ items }: { items: IssueManualItem[] }) {
  if (items.length === 0) return <p className="card-body mb-0">Nothing needs fixing by hand.</p>;
  return (
    <ul className="list-group list-group-flush">
      {items.map((item) => (
        <li key={`${item.reason}:${String(item.project.gitlab_id)}`} className="list-group-item">
          <span className="fw-medium">{item.project.path}</span>
          {`: ${String(item.issues.length)} issues (${item.issues
            .slice(0, 20)
            .map((iid) => `#${String(iid)}`)
            .join(", ")}${item.issues.length > 20 ? ", …" : ""}). ${item.message}`}
        </li>
      ))}
    </ul>
  );
}

const done: Record<PlanAction, string> = {
  submit: "submitted for approval",
  approve: "approved",
  reject: "rejected",
  apply: "is being applied",
};

function send(plan: IssuePlan, user: CurrentUser) {
  return (action: PlanAction, note: string): Promise<IssuePlan> => {
    if (action === "approve") return approveIssuePlan(plan.id, plan.plan_digest, note, user.csrf_token);
    if (action === "reject") return rejectIssuePlan(plan.id, note, user.csrf_token);
    return issuePlanAction(plan.id, action, user.csrf_token);
  };
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
  const [changed, setChanged] = useState<{ plan: IssuePlan; action: PlanAction; base: IssuePlan }>();
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

  const lifecycle = planLifecycle(plan.state);
  const facts: KeyValueItem[] = [
    { term: "State", value: `${planStateLabels[plan.state]}: ${planStateMeaning[plan.state]}` },
    {
      term: "Scope",
      value: plan.scope.groups.length > 0 ? plan.scope.groups.join(", ") : "Every monitored project",
    },
    {
      term: "Rules",
      value: `Label "${plan.rules.missing_time_label}"; estimates rounded up to ${String(plan.rules.estimate_round_minutes)} minutes; ${plan.rules.issue_types.join(", ")}`,
    },
    {
      term: "Steps",
      value: (
        <>
          {`${String(plan.steps)} to apply, ${String(plan.manual)} projects with issues to fix by hand`}
          {(applying || plan.state === "applied" || plan.state === "failed") && (
            <span className="d-block text-secondary" aria-live="polite">
              {`${String(applied)} applied, ${String(skipped)} already in place, ${String(stale)} changed since the plan.`}
            </span>
          )}
        </>
      ),
    },
    { term: "Created", value: `${formatTime(plan.created_at)} by ${personText(plan.created_by)}` },
    { term: "Expires", value: formatTime(plan.expires_at) },
  ];
  if (plan.submitted_at) facts.push({ term: "Submitted", value: formatTime(plan.submitted_at) });
  if (plan.decided_at) {
    facts.push({
      term: plan.state === "rejected" ? "Rejected" : "Decided",
      value: (
        <>
          {plan.self_approved
            ? `${formatTime(plan.decided_at)}: review was off, so it was self-approved`
            : `${formatTime(plan.decided_at)} by ${personText(plan.decided_by)}`}
          {plan.decision_note && (
            <span className="d-block text-secondary" style={{ whiteSpace: "pre-wrap" }}>
              {plan.decision_note}
            </span>
          )}
        </>
      ),
    });
  }
  if (plan.apply_started_at) {
    facts.push({ term: "Applying since", value: formatTime(plan.apply_started_at) });
  }
  if (plan.finished_at) facts.push({ term: "Finished", value: formatTime(plan.finished_at) });

  return (
    <>
      <Lifecycle states={lifecycle.states} current={lifecycle.current} label="Plan progress" />
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="alert alert-success text-body">
          {message}
        </p>
      )}
      {!plan.digest_valid && (
        <p role="alert" className="alert alert-danger text-body">
          The stored plan no longer matches its digest: it was altered after it was created. Do not
          rely on it; it cannot be approved or applied.
        </p>
      )}
      {plan.state === "failed" && (
        <p role="alert" className="alert alert-danger text-body">
          {`Stopped at step ${String(plan.failed_step ?? "?")}: ${plan.failure ?? "an unexpected result"}. Later steps were not run, and no write was retried. Create a new plan.`}
        </p>
      )}
      <div className="row row-cards">
        <div className="col-xl-4 order-xl-2">
          <section aria-labelledby="summary" className="card">
            <div className="card-header">
              <h2 id="summary" className="card-title">
                Summary
              </h2>
            </div>
            <div className="card-body">
              <KeyValue items={facts} />
              <div className="mt-3">
                <DigestDisplay value={plan.plan_digest} label="Plan digest" />
                <p className="small text-secondary mb-0">
                  {plan.digest_valid
                    ? "Recomputed by the API on this read: it matches."
                    : "Recomputed by the API on this read: it does not match."}
                </p>
              </div>
            </div>
          </section>
        </div>
        <div className="col-xl-8 order-xl-1">
          <section aria-labelledby="steps" className="card mb-3">
            <div className="card-header d-block">
              <h2 id="steps" className="card-title">
                Steps
              </h2>
              <p className="text-secondary small mb-0 mt-1">
                Applied in order. Each issue is read first: a change already in place is skipped,
                and an issue that changed since the plan is left alone. Each write is read back to
                verify it; a refused write or a mismatch stops the plan.
              </p>
            </div>
            {plan.step_items.length === 0 ? (
              <p className="card-body mb-0">No changes: everything left needs fixing by hand.</p>
            ) : (
              <ResponsiveTable
                caption="Plan steps, in the order they are applied"
                columns={stepColumns}
                rows={plan.step_items}
                rowKey={(step) => step.step}
              />
            )}
          </section>
          <section aria-labelledby="manual" className="card">
            <div className="card-header d-block">
              <h2 id="manual" className="card-title">
                To fix by hand
              </h2>
              <p className="text-secondary small mb-0 mt-1">
                Issues a plan cannot change: the project does not define the label (Baluarte never
                creates one), or their commits could not be checked yet.
              </p>
            </div>
            <Manual items={plan.manual_items} />
          </section>
        </div>
      </div>
      {user && (
        <PlanDecision
          plan={plan}
          user={user}
          noun="plan"
          stepCount={plan.steps}
          applyBlocks={issueApplyBlocks(plan.can_apply.reasons)}
          canPlan={hasPermission(user, CAN_PLAN)}
          canApprove={hasPermission(user, CAN_APPROVE)}
          maxNote={MAX_NOTE}
          send={send(plan, user)}
          onChanged={(updated, action) => {
            setChanged({ plan: updated, action, base: initial });
          }}
        />
      )}
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
    body = <p className="text-secondary">{`There is no issue plan with ID ${planId}.`}</p>;
  } else if (loaded.error && !data) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (!data) {
    body = <Loading label="Loading plan…" />;
  } else {
    body = <Detail key={data.id} initial={data} created={created} reload={loaded.retry} />;
  }

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to={ISSUE_PLANS}>Closed issue time</Link>}
        title={valid ? `Issue plan ${planId}` : "Issue plan"}
        status={
          data && (
            <Badge tone={planStateTone[data.state]}>{planStateLabels[data.state]}</Badge>
          )
        }
      />
      {body}
    </section>
  );
}
