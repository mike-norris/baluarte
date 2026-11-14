import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router";

import { ApiError } from "../api/client";
import {
  MAX_NOTE,
  changeException,
  getException,
  type ExceptionAction,
  type PolicyException,
  type UserRef,
} from "../api/exceptions";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import type { CurrentUser } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import {
  CAN_APPROVE,
  exceptionStateLabels,
  exceptionStateMeaning,
  hasPermission,
  isOwn,
  requesterName,
} from "../exceptions/format";
import { formatTime } from "../time";
import { DecisionBar } from "../ui/DecisionBar";
import { useDeviceClass } from "../ui/device";
import { Dialog } from "../ui/Dialog";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

function who(user: UserRef | null): string {
  if (!user) return "Baluarte";
  return user.display_name ?? `User ${String(user.user_id)}`;
}

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

interface Step {
  label: string;
  at: string;
  by: string | null;
  note: string | null;
}

/** What happened to the exception, oldest first. */
export function exceptionHistory(exception: PolicyException): Step[] {
  const steps: Step[] = [
    {
      label: "Requested",
      at: exception.requested_at,
      by: requesterName(exception),
      note: exception.justification,
    },
  ];
  if (exception.decided_at) {
    steps.push({
      label: exception.state === "rejected" ? "Rejected" : "Approved",
      at: exception.decided_at,
      by: who(exception.decided_by),
      note: exception.decision_note,
    });
  }
  if (exception.state === "revoked" && exception.ended_at) {
    steps.push({
      label: "Revoked",
      at: exception.ended_at,
      by: who(exception.ended_by),
      note: exception.end_note,
    });
  } else if (exception.state === "expired") {
    steps.push({ label: "Expired", at: exception.expires_at, by: null, note: null });
  }
  return steps;
}

const actionText: Record<
  ExceptionAction,
  { open: string; heading: string; confirm: string; done: string; noteRequired: boolean }
> = {
  approve: {
    open: "Approve…",
    heading: "Approve this exception?",
    confirm: "Confirm approval",
    done: "approved",
    noteRequired: false,
  },
  reject: {
    open: "Reject…",
    heading: "Reject this exception?",
    confirm: "Confirm rejection",
    done: "rejected",
    noteRequired: true,
  },
  revoke: {
    open: "Revoke…",
    heading: "Revoke this exception?",
    confirm: "Confirm revocation",
    done: "revoked",
    noteRequired: true,
  },
};

const actionEffect: Record<ExceptionAction, string> = {
  approve:
    "While it is in force, failures of this rule for this project are recorded as excepted, " +
    "not as passing, until it expires.",
  reject: "Nothing is waived. The requester may submit a new request.",
  revoke: "It ends now: findings it waived are open again while the rule still fails.",
};

function Confirm({
  exception,
  action,
  csrfToken,
  onDone,
  onCancel,
}: {
  exception: PolicyException;
  action: ExceptionAction;
  csrfToken: string;
  onDone: (updated: PolicyException) => void;
  onCancel: () => void;
}) {
  const { refresh } = useAuth();
  const text = actionText[action];
  const [failure, setFailure] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const alert = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (failure) alert.current?.focus();
  }, [failure]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const note = String(new FormData(event.currentTarget).get("note") ?? "");
    if (text.noteRequired && !note.trim()) {
      setFailure("Give a reason; it is recorded with the change.");
      return;
    }
    setSending(true);
    setFailure(null);
    try {
      onDone(await changeException(exception.id, action, note, csrfToken));
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
    <form onSubmit={(event) => void submit(event)} aria-label={text.heading} noValidate>
      <p>{`${actionEffect[action]} This change is audited.`}</p>
      {failure && (
        <p ref={alert} role="alert" tabIndex={-1} className="alert alert-danger text-body">
          {failure}
        </p>
      )}
      <label className="form-label w-100 mb-3">
        {text.noteRequired ? "Reason" : "Note (optional)"}
        <textarea name="note" rows={3} maxLength={MAX_NOTE} className="form-control mt-1" />
      </label>
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
  exception,
  user,
  onChanged,
}: {
  exception: PolicyException;
  user: CurrentUser;
  onChanged: (updated: PolicyException, action: ExceptionAction) => void;
}) {
  const [action, setAction] = useState<ExceptionAction | null>(null);
  const device = useDeviceClass();
  if (!hasPermission(user, CAN_APPROVE)) return null;
  const own = isOwn(exception, user) && user.development_mode !== true;
  const available: ExceptionAction[] = [];
  if (exception.state === "requested" && !own) available.push("approve", "reject");
  if (exception.state === "requested" || exception.state === "approved") available.push("revoke");
  if (available.length === 0 && !own) return null;

  // Phones offer decisions only; revoking is not an approve-or-reject decision.
  const shown = device === "phone" ? available.filter((item) => item !== "revoke") : available;

  return (
    <>
      <DecisionBar
        summary={
          own && exception.state === "requested"
            ? "This is your own request. Someone else must approve or reject it."
            : "Each decision is recorded with its note, and an evaluation is queued."
        }
      >
        {shown.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => {
              setAction(item);
            }}
            className={
              item === "approve" ? "btn btn-primary" : item === "revoke" ? "btn btn-danger" : "btn"
            }
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
            exception={exception}
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

function Detail({ initial }: { initial: PolicyException }) {
  const { session } = useAuth();
  const [changed, setChanged] = useState<{ record: PolicyException; action: ExceptionAction }>();
  const status = useRef<HTMLParagraphElement>(null);
  const exception = changed?.record ?? initial;

  useEffect(() => {
    if (changed) status.current?.focus();
  }, [changed]);

  return (
    <>
      {changed && (
        <p ref={status} role="status" tabIndex={-1} className="alert alert-success text-body">
          {`Exception ${String(exception.id)} ${actionText[changed.action].done}. An evaluation has been queued, so findings follow on its next run. The change is audited.`}
        </p>
      )}
      <section aria-labelledby="summary" className="card mb-3">
        <div className="card-header">
          <h2 id="summary" className="card-title">
            Summary
          </h2>
        </div>
        <dl className="card-body mb-0 py-0">
          <Field term="State">
            {`${exceptionStateLabels[exception.state]}: ${exceptionStateMeaning[exception.state]}`}
          </Field>
          <Field term="Project">
            {exception.project.path}{" "}
            <span className="text-secondary">
              {`(GitLab ID ${String(exception.project.gitlab_id)}, ${exception.project.state})`}
            </span>
          </Field>
          <Field term="Policy">{exception.policy_name}</Field>
          <Field term="Rule">{exception.rule_id}</Field>
          <Field term="Expires">{formatTime(exception.expires_at)}</Field>
          <Field term="Justification">
            <span style={{ whiteSpace: "pre-wrap" }}>{exception.justification}</span>
          </Field>
          <Field term="Findings">
            <Link
              to={`/findings?${new URLSearchParams({
                project: String(exception.project.gitlab_id),
                policy: exception.policy_name,
                rule: exception.rule_id,
              }).toString()}`}
            >
              Findings for this project and rule
            </Link>
          </Field>
        </dl>
      </section>
      <section aria-labelledby="history" className="card mb-3">
        <div className="card-header d-block">
          <h2 id="history" className="card-title">
            History
          </h2>
          <p className="text-secondary small mb-0 mt-1">Oldest first. Every step is audited.</p>
        </div>
        <ol className="list-group list-group-flush">
          {exceptionHistory(exception).map((step) => (
            <li key={step.label} className="list-group-item">
              <p className="mb-0">
                <span className="fw-medium">{step.label}</span>
                {` ${formatTime(step.at)}`}
                {step.by && ` by ${step.by}`}
              </p>
              {step.note && (
                <p className="text-secondary mb-0" style={{ whiteSpace: "pre-wrap" }}>
                  {step.note}
                </p>
              )}
            </li>
          ))}
        </ol>
      </section>
      {session.status === "signed_in" && (
        <Actions
          exception={exception}
          user={session.user}
          onChanged={(record, action) => {
            setChanged({ record, action });
          }}
        />
      )}
    </>
  );
}

export function ExceptionDetailPage() {
  const { exceptionId = "" } = useParams();
  const id = Number(exceptionId);
  const valid = Number.isSafeInteger(id) && id > 0 && String(id) === exceptionId;
  const loaded = useLoad(`exception:${exceptionId}`, (signal) =>
    valid ? getException(id, signal) : Promise.reject(new ApiError(404, "No such exception")),
  );

  let body: ReactNode;
  if (loaded.error?.status === 404) {
    body = <p className="text-secondary">{`There is no exception with ID ${exceptionId}.`}</p>;
  } else if (loaded.error) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (!loaded.data) {
    body = <Loading label="Loading exception…" />;
  } else {
    body = <Detail key={loaded.data.id} initial={loaded.data} />;
  }

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to="/exceptions">Exceptions</Link>}
        title={
          loaded.error?.status === 404
            ? "Exception not found"
            : loaded.data
              ? `Exception ${String(loaded.data.id)}: ${loaded.data.rule_id} on ${loaded.data.project.path}`
              : "Exception"
        }
      />
      {body}
    </section>
  );
}
