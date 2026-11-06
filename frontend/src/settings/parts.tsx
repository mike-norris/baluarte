import { useEffect, useRef, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";
import { secondaryClass } from "../components/styles";

export const errorClass = "rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900";
export const noteClass = "rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950";
export const cardClass = "space-y-4 rounded border border-slate-200 bg-white p-4";

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
      <p className="font-medium">Please fix the following:</p>
      <ul className="mt-1 list-disc pl-5">
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
    <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="font-medium text-slate-700">{label}</dt>
          <dd className="break-all">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function secretLabel(state: "configured" | "not_set"): string {
  return state === "configured" ? "Configured (hidden)" : "Not set";
}

export function groupList(groups: string[]): ReactNode {
  return groups.length > 0 ? groups.join(", ") : <span className="text-slate-500">None</span>;
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
    <div className="space-y-1">
      <p className="text-sm font-medium">{label}</p>
      <div className="flex flex-wrap items-center gap-2">
        <code className="rounded bg-slate-100 px-2 py-1 text-sm break-all">{value}</code>
        <button type="button" onClick={copy} className={secondaryClass}>
          Copy<span className="sr-only"> {label.toLowerCase()}</span>
        </button>
        <span aria-live="polite" className="text-xs text-slate-600">
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
    <p className="rounded border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
      {`${what} is managed by the deployment (environment variables or secret files) and is read-only here. Change it in the deployment configuration and restart Baluarte.`}
    </p>
  );
}

/** Shown when secrets cannot be stored because the encryption key is not mounted. */
export function SecretKeyNotice() {
  return (
    <div className={noteClass}>
      <p className="font-medium">Secrets cannot be saved yet</p>
      <p className="mt-1">
        Baluarte encrypts stored secrets with a key read from the file named by{" "}
        <code>BALUARTE_SECRET_KEY_FILE</code>, and that key is not configured. Generate one and
        restart the API and worker, for example with Docker Compose:
      </p>
      <pre className="mt-2 overflow-x-auto rounded bg-white p-2 text-xs">
        {"(umask 022 && openssl rand -base64 32 > secrets/secret_key)\ndocker compose up -d"}
      </pre>
      <p className="mt-1">Settings without secrets can still be saved.</p>
    </div>
  );
}
