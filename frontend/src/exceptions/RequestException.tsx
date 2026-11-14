import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import {
  MAX_EXCEPTION_DAYS,
  MAX_JUSTIFICATION,
  MIN_JUSTIFICATION,
  requestException,
  type PolicyException,
} from "../api/exceptions";
import type { FindingDetail } from "../api/evaluations";
import { useAuth } from "../auth/context";
import { endOfUtcDay, expiryRange } from "./format";

const errorClass = "alert alert-danger text-body";

/** Problems with the form, in field order; empty when it may be sent. */
export function requestProblems(justification: string, expiry: string, now = Date.now()): string[] {
  const problems: string[] = [];
  const text = justification.trim();
  if (text.length < MIN_JUSTIFICATION) {
    problems.push(`Give a justification of at least ${String(MIN_JUSTIFICATION)} characters.`);
  } else if (text.length > MAX_JUSTIFICATION) {
    problems.push(`Keep the justification to ${String(MAX_JUSTIFICATION)} characters or fewer.`);
  }
  const { min, max } = expiryRange(now);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(expiry)) {
    problems.push("Choose the day the exception expires.");
  } else if (expiry < min || expiry > max) {
    problems.push(
      `Choose an expiry between ${min} and ${max} (at most ${String(MAX_EXCEPTION_DAYS)} days ahead).`,
    );
  }
  return problems;
}

/** The request form for a finding's rule; ``onRequested`` receives the new exception. */
export function RequestExceptionForm({
  finding,
  onRequested,
}: {
  finding: FindingDetail;
  onRequested: (exception: PolicyException) => void;
}) {
  const { session, refresh } = useAuth();
  const [open, setOpen] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const alert = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const range = expiryRange();

  useEffect(() => {
    if (open) heading.current?.focus();
  }, [open]);

  useEffect(() => {
    if (problems.length > 0 || failure) alert.current?.focus();
  }, [problems, failure]);

  if (session.status !== "signed_in") return null;
  const csrfToken = session.user.csrf_token;

  if (!open) {
    return (
      <button
        ref={trigger}
        type="button"
        onClick={() => {
          setOpen(true);
        }}
        className="btn"
      >
        Request exception
      </button>
    );
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const justification = String(form.get("justification") ?? "");
    const expiry = String(form.get("expiry") ?? "");
    const found = requestProblems(justification, expiry);
    setProblems(found);
    setFailure(null);
    if (found.length > 0) return;
    setSending(true);
    try {
      const created = await requestException(
        {
          project_gitlab_id: finding.project.gitlab_id,
          policy_name: finding.policy.name,
          rule_id: finding.rule_id,
          justification: justification.trim(),
          expires_at: endOfUtcDay(expiry),
        },
        csrfToken,
      );
      setOpen(false);
      onRequested(created);
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  const messages = failure ? [failure] : problems;
  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-labelledby="request-exception"
      noValidate
      className="rounded border p-3"
    >
      <h3 id="request-exception" ref={heading} tabIndex={-1} className="fw-medium">
        Request an exception
      </h3>
      <p className="text-secondary">
        {`Waives this failure of ${finding.rule_id} for ${finding.project.path} until it expires. `}
        Someone other than you must approve it, and every step is audited.
      </p>
      {messages.length > 0 && (
        <div ref={alert} role="alert" tabIndex={-1} className={errorClass}>
          <ul className="ps-3">
            {messages.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </div>
      )}
      <label className="d-block fw-medium">
        Justification
        <textarea
          name="justification"
          rows={4}
          maxLength={MAX_JUSTIFICATION}
          aria-describedby="justification-help"
          className="form-control mt-1"
        />
      </label>
      <p id="justification-help" className="small text-secondary">
        {`Why the failure is acceptable for now, and what will fix it. ${String(MIN_JUSTIFICATION)} to ${String(MAX_JUSTIFICATION)} characters.`}
      </p>
      <label className="d-block fw-medium">
        Expires on (UTC)
        <input
          name="expiry"
          type="date"
          min={range.min}
          max={range.max}
          aria-describedby="expiry-help"
          className="form-control mt-1"
        />
      </label>
      <p id="expiry-help" className="small text-secondary">
        {`Ends at 23:59:59 UTC on that day, at most ${String(MAX_EXCEPTION_DAYS)} days ahead. The finding reopens then if the rule still fails.`}
      </p>
      <div className="d-flex gap-2">
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Sending…" : "Submit request"}
        </button>
        <button
          type="button"
          onClick={() => {
            setOpen(false);
            setProblems([]);
            setFailure(null);
          }}
          className="btn"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
