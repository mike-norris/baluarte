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
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import {
  CAN_APPROVE,
  exceptionStateLabels,
  exceptionStateMeaning,
  hasPermission,
  isOwn,
  requesterName,
} from "../exceptions/format";
import { formatTime } from "../time";

const errorClass = "rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900";

function who(user: UserRef | null): string {
  if (!user) return "Baluarte";
  return user.display_name ?? `User ${String(user.user_id)}`;
}

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="py-2 sm:grid sm:grid-cols-3 sm:gap-4">
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="mt-1 sm:col-span-2 sm:mt-0">{children}</dd>
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
  const heading = useRef<HTMLHeadingElement>(null);
  const alert = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    heading.current?.focus();
  }, []);
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
    <form
      onSubmit={(event) => void submit(event)}
      aria-labelledby="confirm-heading"
      noValidate
      className="mt-3 space-y-3 rounded border border-slate-300 p-3"
    >
      <h3 id="confirm-heading" ref={heading} tabIndex={-1} className="font-medium">
        {text.heading}
      </h3>
      <p className="text-sm">{`${actionEffect[action]} This change is audited.`}</p>
      {failure && (
        <p ref={alert} role="alert" tabIndex={-1} className={errorClass}>
          {failure}
        </p>
      )}
      <label className="block text-sm font-medium">
        {text.noteRequired ? "Reason" : "Note (optional)"}
        <textarea name="note" rows={3} maxLength={MAX_NOTE} className={fieldClass} />
      </label>
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
  exception,
  user,
  onChanged,
}: {
  exception: PolicyException;
  user: CurrentUser;
  onChanged: (updated: PolicyException, action: ExceptionAction) => void;
}) {
  const [action, setAction] = useState<ExceptionAction | null>(null);
  if (!hasPermission(user, CAN_APPROVE)) return null;
  const own = isOwn(exception, user);
  const available: ExceptionAction[] = [];
  if (exception.state === "requested" && !own) available.push("approve", "reject");
  if (exception.state === "requested" || exception.state === "approved") available.push("revoke");
  if (available.length === 0 && !own) return null;

  return (
    <section aria-labelledby="actions" className="rounded border border-slate-200 bg-white p-4">
      <h2 id="actions" className="text-lg font-semibold">
        Decision
      </h2>
      {own && exception.state === "requested" && (
        <p className="mt-2 text-sm">
          This is your own request. Someone else must approve or reject it.
        </p>
      )}
      {action ? (
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
      ) : (
        <div className="mt-3 flex gap-2">
          {available.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => {
                setAction(item);
              }}
              className={item === "approve" ? buttonClass : secondaryClass}
            >
              {actionText[item].open}
            </button>
          ))}
        </div>
      )}
    </section>
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
        <p ref={status} role="status" tabIndex={-1} className="text-sm font-medium">
          {`Exception ${String(exception.id)} ${actionText[changed.action].done}. An evaluation has been queued, so findings follow on its next run. The change is audited.`}
        </p>
      )}
      <section aria-labelledby="summary" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="summary" className="text-lg font-semibold">
          Summary
        </h2>
        <dl className="mt-2 divide-y divide-slate-100 text-sm">
          <Field term="State">
            {`${exceptionStateLabels[exception.state]}: ${exceptionStateMeaning[exception.state]}`}
          </Field>
          <Field term="Project">
            {exception.project.path}{" "}
            <span className="text-slate-600">
              {`(GitLab ID ${String(exception.project.gitlab_id)}, ${exception.project.state})`}
            </span>
          </Field>
          <Field term="Policy">{exception.policy_name}</Field>
          <Field term="Rule">{exception.rule_id}</Field>
          <Field term="Expires">{formatTime(exception.expires_at)}</Field>
          <Field term="Justification">
            <span className="whitespace-pre-wrap">{exception.justification}</span>
          </Field>
          <Field term="Findings">
            <Link
              to={`/findings?${new URLSearchParams({
                project: String(exception.project.gitlab_id),
                policy: exception.policy_name,
                rule: exception.rule_id,
              }).toString()}`}
              className={linkClass}
            >
              Findings for this project and rule
            </Link>
          </Field>
        </dl>
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
      <section aria-labelledby="history" className="rounded border border-slate-200 bg-white p-4">
        <h2 id="history" className="text-lg font-semibold">
          History
        </h2>
        <p className="mt-1 text-sm text-slate-600">Oldest first. Every step is audited.</p>
        <ol className="mt-2 space-y-2 text-sm">
          {exceptionHistory(exception).map((step) => (
            <li key={step.label} className="border-l-2 border-slate-300 pl-3">
              <p>
                <span className="font-medium">{step.label}</span>
                {` ${formatTime(step.at)}`}
                {step.by && ` by ${step.by}`}
              </p>
              {step.note && <p className="whitespace-pre-wrap text-slate-700">{step.note}</p>}
            </li>
          ))}
        </ol>
      </section>
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
    body = <p className="text-slate-600">{`There is no exception with ID ${exceptionId}.`}</p>;
  } else if (loaded.error) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (!loaded.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading exception…
      </p>
    );
  } else {
    body = <Detail key={loaded.data.id} initial={loaded.data} />;
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <p className="text-sm">
          <Link to="/exceptions" className={linkClass}>
            Exceptions
          </Link>
        </p>
        <h1 id="page-title" className="mt-1 text-2xl font-semibold">
          {loaded.error?.status === 404
            ? "Exception not found"
            : loaded.data
              ? `Exception ${String(loaded.data.id)}: ${loaded.data.rule_id} on ${loaded.data.project.path}`
              : "Exception"}
        </h1>
      </div>
      {body}
    </section>
  );
}
