import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router";

import { ApiError } from "../api/client";
import {
  applyBlocks,
  approveLabelPlan,
  CAN_APPROVE,
  CAN_PLAN,
  getLabelPlan,
  MAX_NOTE,
  planAction,
  rejectLabelPlan,
  type LabelPlan,
  type LabelPlanStep,
  type ManualItem,
} from "../api/labels";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  actionLabels,
  driftKindLabels,
  LABEL_PLANS,
  ownerHref,
  ownerText,
  planStateLabels,
  planStateMeaning,
  stepStatusLabels,
  valuesText,
} from "../labels/format";
import { personText } from "../policies/format";
import { hasPermission } from "../remediation/format";
import { planLifecycle, planStateTone, stepStatusTone } from "../plans/format";
import { PlanDecision, type PlanAction } from "../plans/PlanDecision";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { DigestDisplay } from "../ui/DigestDisplay";
import { KeyValue, type KeyValueItem } from "../ui/KeyValue";
import { Lifecycle } from "../ui/Lifecycle";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

/** How often a plan being applied is read again. */
export const APPLY_POLL_MS = 3000;

function Lines({ lines }: { lines: string[] }) {
  if (lines.length === 0) return <span className="text-secondary">None</span>;
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

function verifiedText(step: LabelPlanStep): string {
  if (step.verified === null) return step.status === "pending" ? "Not yet" : "Not read back";
  return step.verified ? "Read back: as intended" : "Read back: not as intended";
}

const stepColumns: readonly Column<LabelPlanStep>[] = [
  {
    key: "step",
    header: "Step",
    rowHeader: true,
    render: (step) => <span className="fw-medium">{step.step}</span>,
  },
  {
    key: "where",
    header: "Where",
    render: (step) => <Link to={ownerHref(step.owner)}>{ownerText(step.owner)}</Link>,
  },
  {
    key: "change",
    header: "Change",
    render: (step) => `${actionLabels[step.action]} ${step.label}`,
  },
  {
    key: "before",
    header: "Before",
    render: (step) => <Lines lines={valuesText(step.prior)} />,
    priority: 2,
  },
  {
    key: "after",
    header: "After",
    render: (step) => <Lines lines={valuesText(step.intended)} />,
    priority: 2,
  },
  {
    key: "result",
    header: "Result",
    render: (step) => (
      <div>
        <Badge tone={stepStatusTone[step.status]}>{stepStatusLabels[step.status]}</Badge>
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

function Manual({ items }: { items: ManualItem[] }) {
  if (items.length === 0) return <p className="card-body mb-0">Nothing needs fixing by hand.</p>;
  return (
    <ul className="list-group list-group-flush">
      {items.map((item, index) => (
        <li
          key={`${item.owner.type}:${String(item.owner.gitlab_id)}:${item.kind}:${String(index)}`}
          className="list-group-item"
        >
          <Link to={ownerHref(item.owner)}>{ownerText(item.owner)}</Link>
          {`: ${driftKindLabels[item.kind]}${item.label ? ` (${item.label})` : ""}. ${item.reason}`}
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

function send(plan: LabelPlan, user: CurrentUser) {
  return (action: PlanAction, note: string): Promise<LabelPlan> => {
    if (action === "approve")
      return approveLabelPlan(plan.id, plan.plan_digest, note, user.csrf_token);
    if (action === "reject") return rejectLabelPlan(plan.id, note, user.csrf_token);
    return planAction(plan.id, action, user.csrf_token);
  };
}

function Detail({
  initial,
  created,
  reload,
}: {
  initial: LabelPlan;
  created: boolean;
  reload: () => void;
}) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const [changed, setChanged] = useState<{
    plan: LabelPlan;
    action: PlanAction;
    base: LabelPlan;
  }>();
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
    message = `Plan ${String(plan.id)} created as a draft from labels read just now. Review it, then submit it.`;
  }
  const applied = plan.steps.filter((step) => step.status === "applied").length;
  const skipped = plan.steps.filter((step) => step.status === "skipped").length;
  const lifecycle = planLifecycle(plan.state);

  const facts: KeyValueItem[] = [
    {
      term: "State",
      value: `${planStateLabels[plan.state]}: ${planStateMeaning[plan.state]}`,
    },
    {
      term: "Scope",
      value:
        plan.scope.groups.length > 0 ? plan.scope.groups.join(", ") : "The taxonomy's whole scope",
    },
    {
      term: "Taxonomy",
      value: plan.taxonomy ? `${plan.taxonomy.name} v${String(plan.taxonomy.version)}` : "None",
    },
    {
      term: "Steps",
      value: (
        <>
          {`${String(plan.steps.length)} to apply, ${String(plan.manual.length)} to fix by hand`}
          {(applying || plan.state === "applied" || plan.state === "failed") && (
            <span className="d-block text-secondary" aria-live="polite">
              {`${String(applied)} applied, ${String(skipped)} already as intended.`}
            </span>
          )}
        </>
      ),
    },
    {
      term: "Created",
      value: `${formatTime(plan.created_at)} by ${personText(plan.created_by)}`,
    },
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
    facts.push({
      term: "Applying since",
      value: formatTime(plan.apply_started_at),
    });
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
          {`Stopped at step ${String(plan.failed_step ?? "?")}: ${plan.failure ?? "an unexpected result"}. Later steps were not run, and no write was retried. Create a new plan from a fresh read.`}
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
                Applied in order. Each step is read back first and skipped when already as intended,
                and read back again after the write to verify it. The first unexpected result stops
                the plan.
              </p>
            </div>
            {plan.steps.length === 0 ? (
              <p className="card-body mb-0">No changes: nothing here can be fixed automatically.</p>
            ) : (
              <ResponsiveTable
                caption="Plan steps, in the order they are applied"
                columns={stepColumns}
                rows={plan.steps}
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
                Drift a plan never changes: removing a duplicate or misplaced label, or one not in
                the taxonomy, would need a delete.
              </p>
            </div>
            <Manual items={plan.manual} />
          </section>
        </div>
      </div>
      {user && (
        <PlanDecision
          plan={plan}
          user={user}
          noun="plan"
          stepCount={plan.steps.length}
          applyBlocks={applyBlocks(plan.can_apply.reasons)}
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

export function LabelPlanPage() {
  const { planId = "" } = useParams();
  const location = useLocation();
  const created = (location.state as { created?: boolean } | null)?.created === true;
  const id = Number(planId);
  const valid = Number.isSafeInteger(id) && id > 0 && String(id) === planId;
  const loaded = useLoad(`labels:plan:${planId}`, (signal) =>
    valid ? getLabelPlan(id, signal) : Promise.reject(new ApiError(404, "No such plan")),
  );
  const [shown, setShown] = useState<LabelPlan | undefined>(undefined);
  const data = loaded.data ?? shown;
  if (loaded.data && loaded.data !== shown) setShown(loaded.data);

  let body: ReactNode;
  if (loaded.error?.status === 404) {
    body = <p className="text-secondary">{`There is no label plan with ID ${planId}.`}</p>;
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
        pretitle={<Link to={LABEL_PLANS}>All label plans</Link>}
        title={valid ? `Label plan ${planId}` : "Label plan"}
        status={
          data && <Badge tone={planStateTone[data.state]}>{planStateLabels[data.state]}</Badge>
        }
      />
      {body}
    </section>
  );
}
