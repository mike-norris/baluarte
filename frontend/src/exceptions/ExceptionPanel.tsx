import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { listExceptions, type PolicyException } from "../api/exceptions";
import type { FindingDetail } from "../api/evaluations";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { linkClass } from "../components/styles";
import { outcomeLabels, outcomeMeaning } from "../findings/format";
import { formatTime } from "../time";
import {
  CAN_REQUEST,
  exceptionStateLabels,
  exceptionStateMeaning,
  hasPermission,
  requesterName,
  writeExceptionFilters,
} from "./format";
import { RequestExceptionForm } from "./RequestException";

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="py-2 sm:grid sm:grid-cols-3 sm:gap-4">
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="mt-1 sm:col-span-2 sm:mt-0">{children}</dd>
    </div>
  );
}

function Current({ exception }: { exception: PolicyException }) {
  return (
    <dl className="mt-2 divide-y divide-slate-100 text-sm">
      <Field term="State">
        {`${exceptionStateLabels[exception.state]}: ${exceptionStateMeaning[exception.state]}`}
      </Field>
      <Field term="Requested">
        {`By ${requesterName(exception)} on ${formatTime(exception.requested_at)}`}
      </Field>
      {exception.decided_by && (
        <Field term="Approved">
          {`By ${exception.decided_by.display_name ?? `User ${String(exception.decided_by.user_id)}`} on ${formatTime(exception.decided_at)}`}
        </Field>
      )}
      <Field term="Expires">{formatTime(exception.expires_at)}</Field>
      <Field term="Justification">
        <span className="whitespace-pre-wrap">{exception.justification}</span>
      </Field>
      <Field term="Record">
        <Link to={`/exceptions/${String(exception.id)}`} className={linkClass}>
          {`Exception ${String(exception.id)}`}
        </Link>
      </Field>
    </dl>
  );
}

/** Why no request can be made for this finding, or null when one can. */
function unavailable(finding: FindingDetail, current: PolicyException | undefined): string | null {
  if (current) {
    return `An exception is already ${current.state} for this rule; a new one can be requested after it ends.`;
  }
  const outcome = finding.latest_outcome;
  if (outcome === "fail") return null;
  if (outcome === "unknown" || outcome === "unsupported") {
    return (
      `An exception waives a known failure only. The latest result is ` +
      `${outcomeLabels[outcome].toLowerCase()}: ${outcomeMeaning[outcome]} ` +
      "Fix what stops the rule being checked instead."
    );
  }
  if (outcome === "pass") return "The rule passes, so there is nothing to waive.";
  return "The failure is already covered by an exception.";
}

/** The finding's exception, if any, and the request action for users who may request one. */
export function ExceptionPanel({ finding }: { finding: FindingDetail }) {
  const { session } = useAuth();
  const [requested, setRequested] = useState<PolicyException | null>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const filters = {
    project: finding.project.gitlab_id,
    policy: finding.policy.name,
    rule: finding.rule_id,
  };
  const loaded = useLoad(`exceptions:${writeExceptionFilters(filters).toString()}`, (signal) =>
    listExceptions(filters, null, signal),
  );

  useEffect(() => {
    if (requested) status.current?.focus();
  }, [requested]);

  const user = session.status === "signed_in" ? session.user : null;
  const items = loaded.data?.items ?? [];
  const current = [...items]
    .reverse()
    .find((item) => item.state === "requested" || item.state === "approved");
  const waived = finding.state === "excepted" && finding.exception?.in_force === true;
  const reason = unavailable(finding, current);

  let body: ReactNode;
  if (loaded.error) body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  else if (loaded.loading) {
    body = (
      <p role="status" className="mt-2 text-sm text-slate-600">
        Loading exceptions…
      </p>
    );
  } else {
    body = (
      <>
        {current ? (
          <Current exception={current} />
        ) : (
          <p className="mt-2 text-sm">No exception is requested or in force for this rule.</p>
        )}
        {user && hasPermission(user, CAN_REQUEST) && (
          <div className="mt-3">
            {reason ? (
              <p className="text-sm text-slate-700">{reason}</p>
            ) : (
              <RequestExceptionForm
                finding={finding}
                onRequested={(exception) => {
                  setRequested(exception);
                  loaded.retry();
                }}
              />
            )}
          </div>
        )}
      </>
    );
  }

  return (
    <section aria-labelledby="exception" className="rounded border border-slate-200 bg-white p-4">
      <h2 id="exception" className="text-lg font-semibold">
        Exception
      </h2>
      {waived && finding.exception && (
        <p className="mt-2 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          {`Excepted until ${formatTime(finding.exception.expires_at)}: the failure is recorded as excepted, not as passing. `}
          The finding reopens at expiry, or if the exception is revoked, while the rule still fails.
        </p>
      )}
      {requested && (
        <p ref={status} role="status" tabIndex={-1} className="mt-2 text-sm font-medium">
          {`Exception ${String(requested.id)} requested. It has no effect until someone else approves it.`}
        </p>
      )}
      {body}
      <p className="mt-3 text-sm">
        <Link to={`/exceptions?${writeExceptionFilters(filters).toString()}`} className={linkClass}>
          All exceptions for this project and rule
        </Link>
      </p>
    </section>
  );
}
