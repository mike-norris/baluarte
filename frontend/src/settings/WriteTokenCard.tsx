import { useId, useState, type FormEvent, type ReactNode } from "react";

import type { WriteTokenStatus } from "../api/labels";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { formatTime } from "../time";
import { asApiError, cardClass, noteClass, Problems, SecretKeyNotice, Values } from "./parts";

/** At most this many characters; the API refuses longer tokens. */
export const MAX_TOKEN = 1024;

/** What differs between the label and the remediation write token. */
export interface WriteTokenKind {
  /** Used for element IDs and the load key. */
  id: string;
  title: string;
  intro: ReactNode;
  /** "Label writes", "Remediation writes". */
  writes: string;
  switchName: string;
  help: ReactNode;
  removed: string;
  removeQuestion: string;
  get: (signal?: AbortSignal) => Promise<WriteTokenStatus>;
  put: (token: string, csrf: string) => Promise<WriteTokenStatus>;
  remove: (csrf: string) => Promise<void>;
}

function StatusValues({ kind, status }: { kind: WriteTokenKind; status: WriteTokenStatus }) {
  const rows: [string, ReactNode][] = [
    [
      kind.writes,
      status.writes_enabled ? "Enabled for this deployment" : "Turned off for this deployment",
    ],
    ["Write token", status.set ? "Set (hidden)" : "Not set"],
  ];
  if (status.set) {
    rows.push(["Token name", status.token_name ?? "Not reported"]);
    rows.push([
      "Scopes",
      status.scopes === null
        ? "Not reported"
        : status.scopes.length > 0
          ? status.scopes.join(", ")
          : "None",
    ]);
    rows.push(["Expires", status.expires_at ? formatTime(status.expires_at) : "Not reported"]);
    rows.push(["Last checked", formatTime(status.checked_at)]);
  }
  return <Values rows={rows} />;
}

function TokenForm({
  kind,
  csrf,
  status,
  secretKey,
  onSaved,
}: {
  kind: WriteTokenKind;
  csrf: string;
  status: WriteTokenStatus;
  secretKey: boolean | null;
  onSaved: (message: string) => void;
}) {
  const { refresh } = useAuth();
  const id = useId();
  const [token, setToken] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [sending, setSending] = useState<"save" | "remove" | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const canStore = secretKey !== false;

  function failed(error: unknown) {
    const apiError = asApiError(error);
    setProblems([apiError.message, ...errorReasons(apiError)]);
    if (apiError.status === 401) void refresh();
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = token.trim();
    const found: string[] = [];
    if (!value) found.push("Enter the write token.");
    else if (value.length > MAX_TOKEN) {
      found.push(`The token is longer than ${String(MAX_TOKEN)} characters.`);
    } else if (/\s/.test(value)) found.push("The token cannot contain spaces or line breaks.");
    setProblems(found);
    if (found.length > 0) return;
    setSending("save");
    try {
      const saved = await kind.put(value, csrf);
      setToken("");
      onSaved(
        saved.check_error
          ? `Write token saved, but GitLab could not confirm it: ${saved.check_error}`
          : `Write token ${status.set ? "replaced" : "saved"} and checked with GitLab.`,
      );
    } catch (error) {
      failed(error);
    } finally {
      setSending(null);
    }
  }

  async function remove() {
    setSending("remove");
    setProblems([]);
    try {
      await kind.remove(csrf);
      setConfirmRemove(false);
      onSaved(kind.removed);
    } catch (error) {
      failed(error);
    } finally {
      setSending(null);
    }
  }

  return (
    <div className="stack-4">
      <Problems messages={problems} />
      <form
        className="stack-3"
        onSubmit={(event) => void save(event)}
        aria-label={kind.title}
        noValidate
      >
        <label className="d-block fw-medium">
          {status.set ? "Replace the write token" : "Write token"}
          <input
            name="token"
            type="password"
            value={token}
            onChange={(event) => {
              setToken(event.target.value);
            }}
            autoComplete="new-password"
            disabled={!canStore}
            aria-describedby={`${id}-help`}
            className="form-control mt-1"
          />
        </label>
        <p id={`${id}-help`} className="small text-secondary">
          {kind.help}
        </p>
        {!canStore && <SecretKeyNotice />}
        <div className="d-flex flex-wrap gap-2">
          <button
            type="submit"
            disabled={!canStore || sending !== null}
            className="btn btn-primary"
          >
            {sending === "save"
              ? "Checking and saving…"
              : status.set
                ? "Replace token"
                : "Save token"}
          </button>
          {status.set && !confirmRemove && (
            <button
              type="button"
              onClick={() => {
                setConfirmRemove(true);
              }}
              className="btn"
            >
              Remove token
            </button>
          )}
        </div>
      </form>
      {confirmRemove && (
        <div className={noteClass}>
          <p>{kind.removeQuestion}</p>
          <div className="mt-2 d-flex flex-wrap gap-2">
            <button
              type="button"
              disabled={sending !== null}
              onClick={() => void remove()}
              className="btn btn-primary"
            >
              {sending === "remove" ? "Removing…" : "Remove the token"}
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirmRemove(false);
              }}
              className="btn"
            >
              Keep it
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** A separate, write-capable token and the deployment switch that allows its use. */
export function WriteTokenCard({
  kind,
  csrf,
  secretKey,
}: {
  kind: WriteTokenKind;
  csrf: string;
  secretKey: boolean | null;
}) {
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const loaded = useLoad(`settings-${kind.id}#${String(version)}`, (signal) => kind.get(signal));
  const titleId = `${kind.id}-title`;

  return (
    <section aria-labelledby={titleId} className={cardClass}>
      <h2 id={titleId} className="card-title">
        {kind.title}
      </h2>
      <p className="text-secondary">{kind.intro}</p>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : loaded.data ? (
        <>
          <StatusValues kind={kind} status={loaded.data} />
          {loaded.data.check_error && (
            <p
              className={noteClass}
            >{`GitLab could not confirm the token: ${loaded.data.check_error}`}</p>
          )}
          {!loaded.data.writes_enabled && (
            <p className={noteClass}>
              {`${kind.writes} are turned off for this deployment, so plans cannot be applied even with a token. Set `}
              <code>{`${kind.switchName}=true`}</code>
              {" in the deployment and restart Baluarte to allow them."}
            </p>
          )}
          <TokenForm
            key={String(version)}
            kind={kind}
            csrf={csrf}
            status={loaded.data}
            secretKey={secretKey}
            onSaved={(text) => {
              setMessage(text);
              setVersion((value) => value + 1);
            }}
          />
        </>
      ) : (
        <p role="status" className="text-secondary">
          {`Loading the ${kind.title.toLowerCase()}…`}
        </p>
      )}
      <p role="status" className="text-success-emphasis">
        {message ?? ""}
      </p>
    </section>
  );
}
