import { useId, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { requestControlException, type ControlException } from "../api/compliance";
import { fieldClass } from "../components/styles";
import { asApiError, cardClass, Problems } from "../settings/parts";
import { isoDate } from "./format";

export const MAX_EXCEPTION_DAYS = 365;

/** A sentence for what happens next with a requested exception. */
export function requestedMessage(exception: ControlException): string {
  return exception.in_force
    ? `Exception ${String(exception.id)} is in force until ${exception.expires_on}.`
    : `Exception ${String(exception.id)} is waiting for another administrator to approve it on the Compliance page.`;
}

/** Request a time-limited exception to one control of the operating profile. */
export function ExceptionRequestForm({
  controls,
  controlId,
  csrf,
  onRequested,
  onCancel,
  onError,
  today = new Date(),
}: {
  controls: { id: string; title: string }[];
  controlId?: string;
  csrf: string;
  onRequested: (exception: ControlException) => void;
  onCancel: () => void;
  onError?: (error: ApiError) => void;
  today?: Date;
}) {
  const [control, setControl] = useState(controlId ?? controls[0]?.id ?? "");
  const [justification, setJustification] = useState("");
  const [expiresOn, setExpiresOn] = useState(isoDate(today, 90));
  const [problems, setProblems] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const id = useId();
  const earliest = isoDate(today, 1);
  const latest = isoDate(today, MAX_EXCEPTION_DAYS);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found: string[] = [];
    if (!control) found.push("Choose a control.");
    const reason = justification.trim();
    if (reason.length < 10 || reason.length > 2000) {
      found.push("Give a justification of 10 to 2,000 characters.");
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(expiresOn) || expiresOn < earliest || expiresOn > latest) {
      found.push(`Choose an expiry date from ${earliest} to ${latest}.`);
    }
    setProblems(found);
    if (found.length > 0) return;
    setSending(true);
    try {
      onRequested(
        await requestControlException(
          { control_id: control, justification: reason, expires_on: expiresOn },
          csrf,
        ),
      );
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message]);
      onError?.(apiError);
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      noValidate
      aria-label="Request an exception"
      className={cardClass}
    >
      <h3 className="fw-semibold">Request an exception</h3>
      <p className="text-secondary">
        An exception lets Baluarte run below one control&apos;s requirement until it expires. It is
        audited and appears in evidence exports. While change review is required, another
        administrator must approve it.
      </p>
      <Problems messages={problems} />
      <label className="d-block fw-medium">
        Control
        <select
          value={control}
          disabled={controlId !== undefined}
          onChange={(event) => {
            setControl(event.target.value);
          }}
          className="form-control mt-1"
        >
          {controls.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
            </option>
          ))}
        </select>
      </label>
      <div>
        <label htmlFor={`${id}-justification`} className="d-block fw-medium">
          Justification
        </label>
        <textarea
          id={`${id}-justification`}
          aria-describedby={`${id}-justification-hint`}
          value={justification}
          onChange={(event) => {
            setJustification(event.currentTarget.value);
          }}
          rows={3}
          maxLength={2000}
          className="form-control mt-1"
        />
        <p id={`${id}-justification-hint`} className="small text-secondary">
          Why the requirement cannot be met now, and what reduces the risk meanwhile.
        </p>
      </div>
      <div>
        <label htmlFor={`${id}-expires`} className="d-block fw-medium">
          Expires on
        </label>
        <input
          id={`${id}-expires`}
          aria-describedby={`${id}-expires-hint`}
          type="date"
          value={expiresOn}
          min={earliest}
          max={latest}
          onChange={(event) => {
            setExpiresOn(event.currentTarget.value);
          }}
          className={`${fieldClass} max-w-48`}
        />
        <p id={`${id}-expires-hint`} className="small text-secondary">
          At most {MAX_EXCEPTION_DAYS} days from today.
        </p>
      </div>
      <div className="d-flex flex-wrap gap-2">
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Requesting…" : "Request exception"}
        </button>
        <button type="button" onClick={onCancel} className="btn">
          Cancel
        </button>
      </div>
    </form>
  );
}
