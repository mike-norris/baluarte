import { useEffect, useRef, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";

export const errorClass = "alert alert-danger text-body";
export const noteClass = "alert alert-warning text-body";
export const cardClass = "card card-body mb-3";

/** An ApiError for anything thrown by a request. */
export function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
}

/** An error summary that takes focus when it appears, so keyboard and screen reader users hear it. */
export function Problems({ messages }: { messages: string[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const text = messages.join("\n");

  useEffect(() => {
    if (text) ref.current?.focus();
  }, [text]);

  if (messages.length === 0) return null;
  return (
    <div ref={ref} role="alert" tabIndex={-1} className={errorClass}>
      <p className="fw-medium">Please fix the following:</p>
      <ul className="mt-1 ps-3">
        {messages.map((message) => (
          <li key={message}>{message}</li>
        ))}
      </ul>
    </div>
  );
}

/** Read-only values as a description list. */
export function Values({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid column-gap-3 gap-y-2 sm:grid-cols-[max-content_1fr]">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="col-md-4 fw-medium text-secondary">{label}</dt>
          <dd className="text-break">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function secretLabel(state: "configured" | "not_set"): string {
  return state === "configured" ? "Configured (hidden)" : "Not set";
}

export function groupList(groups: string[]): ReactNode {
  return groups.length > 0 ? groups.join(", ") : <span className="text-secondary">None</span>;
}

/** A value with a button that copies it; the result is announced politely. */
export function CopyValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);

  function copy() {
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (!clipboard) {
      setCopied("no");
      return;
    }
    clipboard.writeText(value).then(
      () => {
        setCopied("yes");
      },
      () => {
        setCopied("no");
      },
    );
  }

  return (
    <div >
      <p className="fw-medium">{label}</p>
      <div className="d-flex flex-wrap align-items-center gap-2">
        <code className="badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle">{value}</code>
        <button type="button" onClick={copy} className="btn">
          Copy<span className="visually-hidden"> {label.toLowerCase()}</span>
        </button>
        <span aria-live="polite" className="small text-secondary">
          {copied === "yes"
            ? "Copied."
            : copied === "no"
              ? "Copy failed; select the text instead."
              : ""}
        </span>
      </div>
    </div>
  );
}

/** Explains that a section is set by the deployment and cannot be changed here. */
export function DeploymentNotice({ what }: { what: string }) {
  return (
    <p className="alert alert-secondary text-body">
      {`${what} is managed by the deployment (environment variables or secret files) and is read-only here. Change it in the deployment configuration and restart Baluarte.`}
    </p>
  );
}

/** Shown when secrets cannot be stored because the encryption key is not mounted. */
export function SecretKeyNotice() {
  return (
    <div className={noteClass}>
      <p className="fw-medium">Secrets cannot be saved yet</p>
      <p className="mt-1">
        Baluarte encrypts stored secrets with a key read from the file named by{" "}
        <code>BALUARTE_SECRET_KEY_FILE</code>, and that key is not configured. Generate one and
        restart the API and worker, for example with Docker Compose:
      </p>
      <pre className="mt-2 overflow-x-auto rounded p-2 small">
        {"(umask 022 && openssl rand -base64 32 > secrets/secret_key)\ndocker compose up -d"}
      </pre>
      <p className="mt-1">Settings without secrets can still be saved.</p>
    </div>
  );
}
