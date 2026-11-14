import { IconCheck, IconPlayerPlay, IconSend, IconX } from "@tabler/icons-react";
import { useId, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";
import type { Person } from "../api/policies";
import { useAuth } from "../auth/context";
import { leavesOwnToOthers, type CurrentUser } from "../auth/session";
import { useAsOfState } from "../asof/context";
import { pastReadOnly } from "../asof/format";
import { LARGER_SCREEN } from "../ui/CapabilityGate";
import { DecisionBar } from "../ui/DecisionBar";
import { Dialog } from "../ui/Dialog";
import { useDeviceClass } from "../ui/device";

export type PlanAction = "submit" | "approve" | "reject" | "apply";

/** What PlanDecision needs to know about a label or issue plan. */
export interface DecidablePlan {
  id: number;
  state: string;
  plan_digest: string;
  digest_valid: boolean;
  review_required: boolean;
  created_by: Person;
  can_apply: { allowed: boolean };
}

interface PlanDecisionProps<Plan extends DecidablePlan> {
  plan: Plan;
  user: CurrentUser;
  /** "label plan" or "issue plan". */
  noun: string;
  /** Changes the plan would make, for the confirmations. */
  stepCount: number;
  /** Why it cannot be applied now, as sentences. */
  applyBlocks: string[];
  canPlan: boolean;
  canApprove: boolean;
  maxNote: number;
  send: (action: PlanAction, note: string) => Promise<Plan>;
  onChanged: (plan: Plan, action: PlanAction) => void;
}

const titles: Record<PlanAction, string> = {
  submit: "Submit this plan for approval?",
  approve: "Approve this plan?",
  reject: "Reject this plan?",
  apply: "Apply this plan in GitLab?",
};

const confirmLabels: Record<PlanAction, string> = {
  submit: "Confirm submission",
  approve: "Confirm approval",
  reject: "Confirm rejection",
  apply: "Confirm apply",
};

const sendingLabels: Record<PlanAction, string> = {
  submit: "Submitting…",
  approve: "Approving…",
  reject: "Rejecting…",
  apply: "Starting…",
};

function plural(count: number, one: string): string {
  return `${String(count)} ${one}${count === 1 ? "" : "s"}`;
}

/**
 * Submit, approve, reject and apply for a reviewed plan. The buttons stay in view in a decision
 * bar; each opens a dialog that restates what is decided before anything is sent. On phones
 * only approve and reject are offered.
 */
export function PlanDecision<Plan extends DecidablePlan>({
  plan,
  user,
  noun,
  stepCount,
  applyBlocks,
  canPlan,
  canApprove,
  maxNote,
  send,
  onChanged,
}: PlanDecisionProps<Plan>) {
  const { refresh } = useAuth();
  const { chosen } = useAsOfState();
  const device = useDeviceClass();
  const id = useId();
  const [open, setOpen] = useState<PlanAction | null>(null);
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const creator = plan.created_by.user_id === user.user_id;
  const ownDecision = leavesOwnToOthers(user, plan.created_by.user_id);
  const past = chosen !== null;

  const blocks = [...applyBlocks];
  if (past) blocks.push(pastReadOnly);
  const applyAllowed = plan.can_apply.allowed && !past;
  const showSubmit = plan.state === "draft" && canPlan && creator;
  const mayDecide = plan.state === "submitted" && canApprove;
  const showApply = canPlan && (plan.state === "approved" || plan.state === "submitted");
  if (!showSubmit && !mayDecide && !showApply) return null;

  const decisionOpen = mayDecide && !ownDecision && plan.digest_valid;
  const steps = plural(stepCount, "change");

  function start(action: PlanAction) {
    setFailure(null);
    setNote("");
    setOpen(action);
  }

  async function confirm() {
    if (open === null) return;
    if (open === "reject" && !note.trim()) {
      setFailure("Explain why in the note before rejecting.");
      return;
    }
    setSending(true);
    setFailure(null);
    try {
      const updated = await send(open, note);
      const action = open;
      setOpen(null);
      onChanged(updated, action);
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  let summary: ReactNode;
  if (mayDecide && ownDecision) {
    summary = `You created this ${noun}. Someone else must approve or reject it.`;
  } else if (mayDecide && !plan.digest_valid) {
    summary = "The stored plan no longer matches its digest, so it cannot be approved.";
  } else if (decisionOpen) {
    summary = (
      <>
        {`Approving binds to digest `}
        <code className="bg-transparent p-0">{`${plan.plan_digest.slice(0, 12)}…`}</code>
        {` (${steps}). Nothing changes in GitLab until the plan is applied.`}
      </>
    );
  } else if (showSubmit) {
    summary = plan.review_required
      ? "Submitting asks someone other than you to approve it."
      : "Review is off: submitting approves it at once, recorded as self-approved.";
  } else if (showApply && !applyAllowed) {
    summary = (
      <div id={`${id}-why-not`}>
        <p className="fw-medium mb-1">It can't be applied now:</p>
        <ul className="mb-0 ps-3">
          {blocks.map((block) => (
            <li key={block}>{block}</li>
          ))}
        </ul>
      </div>
    );
  } else {
    summary = `Applying writes ${steps} to GitLab, each read back to verify it.`;
  }

  const body: Record<PlanAction, ReactNode> = {
    submit: (
      <p>
        {plan.review_required
          ? `Plan ${String(plan.id)} (${steps}) goes to someone other than you for approval.`
          : `Review is off: plan ${String(plan.id)} (${steps}) is approved at once and recorded as self-approved.`}
      </p>
    ),
    approve: (
      <>
        <p>{`Your approval covers exactly these ${steps} and is refused if the plan has changed. Nothing changes in GitLab until it is applied.`}</p>
        <p className="mb-1 subheader">Plan digest</p>
        <code className="d-block text-break bg-transparent p-0 mb-3">{plan.plan_digest}</code>
      </>
    ),
    reject: <p>{`Plan ${String(plan.id)} is closed and cannot be applied. Say why in the note.`}</p>,
    apply: (
      <p>
        {`${steps} will be written to GitLab in order. Each is read back first and skipped when already as intended, then read back again to verify it. The first unexpected result stops the plan.`}
      </p>
    ),
  };

  return (
    <>
      <DecisionBar summary={summary}>
        {showSubmit && (
          device !== "phone" && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={past}
              onClick={() => {
                start("submit");
              }}
            >
              <IconSend size={18} className="icon" aria-hidden="true" />
              Submit for approval
            </button>
          )
        )}
        {decisionOpen && (
          <>
            <button
              type="button"
              className="btn"
              disabled={past}
              onClick={() => {
                start("reject");
              }}
            >
              <IconX size={18} className="icon" aria-hidden="true" />
              Reject
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={past}
              onClick={() => {
                start("approve");
              }}
            >
              <IconCheck size={18} className="icon" aria-hidden="true" />
              Approve this digest
            </button>
          </>
        )}
        {showApply &&
          (device === "phone" ? (
            <span className="text-secondary small">{`Apply: ${LARGER_SCREEN}`}</span>
          ) : (
            <button
              type="button"
              className="btn btn-primary"
              disabled={!applyAllowed}
              aria-describedby={applyAllowed ? undefined : `${id}-why-not`}
              onClick={() => {
                start("apply");
              }}
            >
              <IconPlayerPlay size={18} className="icon" aria-hidden="true" />
              Apply in GitLab
            </button>
          ))}
      </DecisionBar>
      <Dialog
        open={open !== null}
        title={open ? titles[open] : ""}
        onClose={() => {
          if (!sending) setOpen(null);
        }}
        footer={
          <>
            <button
              type="button"
              className="btn"
              disabled={sending}
              onClick={() => {
                setOpen(null);
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={sending}
              onClick={() => void confirm()}
            >
              {open && (sending ? sendingLabels[open] : confirmLabels[open])}
            </button>
          </>
        }
      >
        {open && body[open]}
        {(open === "approve" || open === "reject") && (
          <div>
            <label htmlFor={`${id}-note`} className="form-label">
              {open === "reject" ? "Note (required to reject)" : "Note (optional)"}
            </label>
            <textarea
              id={`${id}-note`}
              value={note}
              maxLength={maxNote}
              rows={3}
              className="form-control"
              onChange={(event) => {
                setNote(event.target.value);
              }}
            />
          </div>
        )}
        {failure && (
          <p role="alert" className="text-danger-emphasis mt-3 mb-0">
            {failure}
          </p>
        )}
      </Dialog>
    </>
  );
}
