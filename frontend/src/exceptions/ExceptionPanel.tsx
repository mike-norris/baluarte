import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { listExceptions, type PolicyException } from "../api/exceptions";
import type { FindingDetail } from "../api/evaluations";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { PastReadOnly } from "../asof/PastReadOnly";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
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
    <div className="row g-2 py-2 border-bottom">
      <dt className="col-md-4 fw-medium text-secondary">{term}</dt>
      <dd className="col-md-8 mb-0">{children}</dd>
    </div>
  );
}

function Current({ exception }: { exception: PolicyException }) {
  return (
    <dl className="mb-0">
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
        <span className="text-prewrap">{exception.justification}</span>
      </Field>
      <Field term="Record">
        <Link to={`/exceptions/${String(exception.id)}`}>
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
  const asOf = useAsOf();
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
      <p role="status" className="mt-2 text-secondary">
        Loading exceptions…
      </p>
    );
  } else {
    body = (
      <>
        {current ? (
          <Current exception={current} />
        ) : (
          <p className="mt-2">No exception is requested or in force for this rule.</p>
        )}
        {user && hasPermission(user, CAN_REQUEST) && (
          <div className="mt-3">
            {asOf ? (
              <PastReadOnly />
            ) : reason ? (
              <p className="text-secondary">{reason}</p>
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
    <section aria-labelledby="exception" className="card card-body mb-3">
      <h2 id="exception" className="card-title">
        Exception
      </h2>
      {asOf && <p className="mt-1 text-secondary">Exceptions are shown as they are now.</p>}
      {waived && finding.exception && (
        <p className="alert alert-warning text-body">
          {`Excepted until ${formatTime(finding.exception.expires_at)}: the failure is recorded as excepted, not as passing. `}
          The finding reopens at expiry, or if the exception is revoked, while the rule still fails.
        </p>
      )}
      {requested && (
        <p ref={status} role="status" tabIndex={-1} className="mt-2 fw-medium">
          {`Exception ${String(requested.id)} requested. It has no effect until someone else approves it.`}
        </p>
      )}
      {body}
      <p className="mt-3">
        <Link to={`/exceptions?${writeExceptionFilters(filters).toString()}`}>
          All exceptions for this project and rule
        </Link>
      </p>
    </section>
  );
}
