import { useId, useState, type FormEvent, type ReactNode } from "react";

import {
  getWriteToken,
  putWriteToken,
  removeWriteToken,
  type WriteTokenStatus,
} from "../api/labels";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import { formatTime } from "../time";
import { asApiError, cardClass, noteClass, Problems, SecretKeyNotice, Values } from "./parts";

/** At most this many characters; the API refuses longer tokens. */
export const MAX_TOKEN = 1024;

function StatusValues({ status }: { status: WriteTokenStatus }) {
  const rows: [string, ReactNode][] = [
    ["Label writes", status.writes_enabled ? "Enabled for this deployment" : "Turned off for this deployment"],
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
  csrf,
  status,
  secretKey,
  onSaved,
}: {
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
    }
    setProblems(found);
    if (found.length > 0) return;
    setSending("save");
    try {
      const saved = await putWriteToken(value, csrf);
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
      await removeWriteToken(csrf);
      setConfirmRemove(false);
      onSaved("Write token removed. Label plans cannot be applied until a new one is set.");
    } catch (error) {
      failed(error);
    } finally {
      setSending(null);
    }
  }

  return (
    <div className="space-y-4">
      <Problems messages={problems} />
      <form onSubmit={(event) => void save(event)} aria-label="Label write token" noValidate className="space-y-3">
        <label className="block text-sm font-medium">
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
            className={fieldClass}
          />
        </label>
        <p id={`${id}-help`} className="text-xs text-slate-600">
          Never shown again once saved. Use a group access token with the Maintainer role and the{" "}
          <code>api</code> scope, limited to the groups whose labels Baluarte should fix, with an
          expiry date. It is used only to apply approved label plans; every other read keeps using
          the read-only token.
        </p>
        {!canStore && <SecretKeyNotice />}
        <div className="flex flex-wrap gap-2">
          <button type="submit" disabled={!canStore || sending !== null} className={buttonClass}>
            {sending === "save" ? "Checking and saving…" : status.set ? "Replace token" : "Save token"}
          </button>
          {status.set && !confirmRemove && (
            <button
              type="button"
              onClick={() => {
                setConfirmRemove(true);
              }}
              className={secondaryClass}
            >
              Remove token
            </button>
          )}
        </div>
      </form>
      {confirmRemove && (
        <div className={noteClass}>
          <p>Remove the write token? Approved label plans cannot be applied without one.</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={sending !== null}
              onClick={() => void remove()}
              className={buttonClass}
            >
              {sending === "remove" ? "Removing…" : "Remove the token"}
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirmRemove(false);
              }}
              className={secondaryClass}
            >
              Keep it
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** The separate, write-capable token that applies approved label plans (ADR-0009). */
export function LabelWriteTokenCard({
  csrf,
  secretKey,
}: {
  csrf: string;
  secretKey: boolean | null;
}) {
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const loaded = useLoad(`settings-label-write-token#${String(version)}`, (signal) =>
    getWriteToken(signal),
  );

  return (
    <section aria-labelledby="label-token-title" className={cardClass}>
      <h2 id="label-token-title" className="text-lg font-semibold">
        Label write token
      </h2>
      <p className="text-sm text-slate-600">
        The only credential Baluarte writes to GitLab with, and only to create, rename or update
        labels from an approved plan. It never deletes. Setting, replacing and removing it are
        recorded in the audit log.
      </p>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : loaded.data ? (
        <>
          <StatusValues status={loaded.data} />
          {loaded.data.check_error && (
            <p className={noteClass}>{`GitLab could not confirm the token: ${loaded.data.check_error}`}</p>
          )}
          {!loaded.data.writes_enabled && (
            <p className={noteClass}>
              Label writes are turned off for this deployment, so plans cannot be applied even with
              a token. Set <code>BALUARTE_LABEL_WRITES=true</code> in the deployment and restart
              Baluarte to allow them.
            </p>
          )}
          <TokenForm
            key={String(version)}
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
        <p role="status" className="text-sm text-slate-600">
          Loading the label write token…
        </p>
      )}
      <p role="status" className="text-sm text-emerald-800">
        {message ?? ""}
      </p>
    </section>
  );
}
