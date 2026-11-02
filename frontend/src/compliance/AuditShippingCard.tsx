import { useId, useState, type FormEvent, type ReactNode } from "react";

import {
  getAuditShipping,
  getShippingStatus,
  saveAuditShipping,
  testAuditShipping,
  type AuditShippingConfig,
  type AuditShippingUpdate,
  type ShippingStatus,
  type ShippingTestResult,
  type ShippingType,
} from "../api/compliance";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, secondaryClass } from "../components/styles";
import {
  asApiError,
  cardClass,
  errorClass,
  Problems,
  secretLabel,
  Values,
} from "../settings/parts";
import { formatTime } from "../time";

interface Draft {
  enabled: boolean;
  type: ShippingType;
  host: string;
  port: string;
  serverName: string;
  syslogCa: string;
  url: string;
  index: string;
  auth: "api_key" | "basic";
  username: string;
  apiKey: string;
  password: string;
  esCa: string;
}

function draftFrom(config: AuditShippingConfig): Draft {
  return {
    enabled: config.enabled,
    type: config.type,
    host: config.syslog.host,
    port: String(config.syslog.port),
    serverName: config.syslog.server_name ?? "",
    syslogCa: config.syslog.ca_pem ?? "",
    url: config.elasticsearch.url,
    index: config.elasticsearch.index,
    auth: config.elasticsearch.auth,
    username: config.elasticsearch.username ?? "",
    apiKey: "",
    password: "",
    esCa: config.elasticsearch.ca_pem ?? "",
  };
}

function orNull(value: string): string | null {
  return value.trim() ? value.trim() : null;
}

export function toUpdate(draft: Draft): AuditShippingUpdate {
  return {
    enabled: draft.enabled,
    type: draft.type,
    syslog: {
      host: draft.host.trim(),
      port: Number(draft.port),
      server_name: orNull(draft.serverName),
      ca_pem: orNull(draft.syslogCa),
    },
    elasticsearch: {
      url: draft.url.trim(),
      index: draft.index.trim(),
      auth: draft.auth,
      username: draft.auth === "basic" ? orNull(draft.username) : null,
      ca_pem: orNull(draft.esCa),
    },
    api_key:
      draft.type === "elasticsearch" && draft.auth === "api_key" ? orNull(draft.apiKey) : null,
    password:
      draft.type === "elasticsearch" && draft.auth === "basic" ? orNull(draft.password) : null,
  };
}

/** What is wrong with the destination; checked for a test as well as for an enabled save. */
export function destinationProblems(draft: Draft, stored: AuditShippingConfig): string[] {
  const problems: string[] = [];
  if (draft.type === "syslog_tls") {
    if (!draft.host.trim()) problems.push("Syslog host: enter the collector's host name.");
    const port = Number(draft.port);
    if (!/^\d+$/.test(draft.port.trim()) || port < 1 || port > 65535) {
      problems.push("Syslog port: enter a port from 1 to 65535.");
    }
    return problems;
  }
  const url = draft.url.trim();
  if (!/^https:\/\/[^\s/]+/i.test(url)) {
    problems.push("Address: enter an https:// address. Audit events are never sent unencrypted.");
  }
  if (!draft.index.trim()) problems.push("Index: enter an index name.");
  const moved = url !== stored.elasticsearch.url;
  if (draft.auth === "api_key") {
    if (!draft.apiKey.trim() && (stored.api_key === "not_set" || moved)) {
      problems.push(
        moved && stored.api_key === "configured"
          ? "API key: enter it again, because the address changed."
          : "API key: enter one.",
      );
    }
  } else {
    if (!draft.username.trim()) problems.push("Username: enter one.");
    if (!draft.password.trim() && (stored.password === "not_set" || moved)) {
      problems.push(
        moved && stored.password === "configured"
          ? "Password: enter it again, because the address changed."
          : "Password: enter one.",
      );
    }
  }
  return problems;
}

function Field({
  label,
  hint,
  children,
  id,
}: {
  label: string;
  hint?: string | undefined;
  children: (props: { id: string; "aria-describedby"?: string }) => ReactNode;
  id: string;
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      {children({ id, ...(hint ? { "aria-describedby": `${id}-hint` } : {}) })}
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-slate-600">
          {hint}
        </p>
      )}
    </div>
  );
}

function StatusPanel({ status }: { status: ShippingStatus }) {
  if (!status.enabled) {
    return (
      <p className="text-sm text-slate-700">Shipping is off. Audit events stay only in Baluarte.</p>
    );
  }
  const behind =
    status.lag_events === 0
      ? "Up to date"
      : `${String(status.lag_events)} events waiting${status.oldest_unshipped_at ? `, the oldest from ${formatTime(status.oldest_unshipped_at)}` : ""}`;
  const rows: [string, ReactNode][] = [
    ["Destination", status.destination ?? ""],
    ["Delivery", behind],
    [
      "Last delivered",
      status.last_success_at
        ? `${formatTime(status.last_success_at)}${status.last_shipped_id === null ? "" : ` (up to event ${String(status.last_shipped_id)})`}`
        : "Nothing delivered yet",
    ],
  ];
  if (status.last_error) {
    rows.push([
      "Last error",
      `${status.last_error}${status.consecutive_failures > 0 ? ` (${String(status.consecutive_failures)} failed attempts in a row)` : ""}`,
    ]);
  }
  return (
    <div aria-label="Shipping status" role="group" className="space-y-2">
      <Values rows={rows} />
    </div>
  );
}

function ShippingForm({
  config,
  csrf,
  onSaved,
  onCancel,
}: {
  config: AuditShippingConfig;
  csrf: string;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const { refresh } = useAuth();
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => draftFrom(config));
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [test, setTest] = useState<ShippingTestResult | null>(null);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  function failed(error: unknown) {
    const apiError = asApiError(error);
    setProblems([apiError.message]);
    if (apiError.status === 401) void refresh();
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTest(null);
    const found = draft.enabled ? destinationProblems(draft, config) : [];
    setProblems(found);
    if (found.length > 0) return;
    setBusy("save");
    try {
      await saveAuditShipping(toUpdate(draft), csrf);
      onSaved();
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
    }
  }

  async function sendTest() {
    setTest(null);
    const found = destinationProblems(draft, config);
    setProblems(found);
    if (found.length > 0) return;
    setBusy("test");
    try {
      setTest(await testAuditShipping(toUpdate(draft), csrf));
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
    }
  }

  const textField = (
    key: "host" | "port" | "serverName" | "url" | "index" | "username",
    label: string,
    hint?: string,
    inputMode?: "numeric",
  ) => (
    <Field id={`${id}-${key}`} label={label} hint={hint}>
      {(props) => (
        <input
          {...props}
          type="text"
          inputMode={inputMode}
          value={draft[key]}
          onChange={(event) => {
            set(key, event.currentTarget.value);
          }}
          className={fieldClass}
        />
      )}
    </Field>
  );

  const caField = (key: "syslogCa" | "esCa") => (
    <Field
      id={`${id}-${key}`}
      label="CA certificate (PEM, optional)"
      hint="Only for a private certificate authority. The server's certificate is always verified."
    >
      {(props) => (
        <textarea
          {...props}
          value={draft[key]}
          rows={4}
          spellCheck={false}
          onChange={(event) => {
            set(key, event.currentTarget.value);
          }}
          className={`${fieldClass} font-mono text-xs`}
        />
      )}
    </Field>
  );

  const secretField = (
    key: "apiKey" | "password",
    label: string,
    state: "configured" | "not_set",
  ) => (
    <Field
      id={`${id}-${key}`}
      label={label}
      hint={
        state === "configured"
          ? "Stored and hidden. Leave blank to keep it."
          : "Stored encrypted; never shown again."
      }
    >
      {(props) => (
        <input
          {...props}
          type="password"
          autoComplete="off"
          value={draft[key]}
          onChange={(event) => {
            set(key, event.currentTarget.value);
          }}
          className={fieldClass}
        />
      )}
    </Field>
  );

  return (
    <form onSubmit={(event) => void save(event)} noValidate className="space-y-4">
      <Problems messages={problems} />
      <label className="flex items-center gap-2 text-sm font-medium">
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(event) => {
            set("enabled", event.currentTarget.checked);
          }}
          className="h-4 w-4"
        />
        Ship audit events to this destination
      </label>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Destination</legend>
        {(
          [
            ["syslog_tls", "Syslog over TLS (RFC 5424 and 5425)"],
            ["elasticsearch", "Elasticsearch or OpenSearch"],
          ] as const
        ).map(([value, label]) => (
          <label key={value} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={`${id}-type`}
              value={value}
              checked={draft.type === value}
              onChange={() => {
                set("type", value);
              }}
              className="h-4 w-4"
            />
            {label}
          </label>
        ))}
      </fieldset>
      {draft.type === "syslog_tls" ? (
        <div className="space-y-3">
          {textField("host", "Syslog host")}
          {textField("port", "Port", "6514 is the standard port for syslog over TLS.", "numeric")}
          {textField(
            "serverName",
            "TLS server name (optional)",
            "Only when the certificate names a different host than the one above.",
          )}
          {caField("syslogCa")}
        </div>
      ) : (
        <div className="space-y-3">
          {textField("url", "Address", "For example https://logs.example.internal:9200")}
          {textField("index", "Index")}
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Authentication</legend>
            {(
              [
                ["api_key", "API key"],
                ["basic", "Username and password"],
              ] as const
            ).map(([value, label]) => (
              <label key={value} className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name={`${id}-auth`}
                  value={value}
                  checked={draft.auth === value}
                  onChange={() => {
                    set("auth", value);
                  }}
                  className="h-4 w-4"
                />
                {label}
              </label>
            ))}
          </fieldset>
          {draft.auth === "api_key" ? (
            secretField("apiKey", "API key", config.api_key)
          ) : (
            <>
              {textField("username", "Username")}
              {secretField("password", "Password", config.password)}
            </>
          )}
          {caField("esCa")}
        </div>
      )}
      {test && (
        <p
          role={test.ok ? "status" : "alert"}
          className={
            test.ok
              ? "rounded border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900"
              : errorClass
          }
        >
          {test.ok ? `Test message delivered. ${test.detail}` : `Test failed: ${test.detail}`}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={busy !== null} className={buttonClass}>
          {busy === "save" ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void sendTest()}
          className={secondaryClass}
        >
          {busy === "test" ? "Sending…" : "Send test message"}
        </button>
        <button type="button" onClick={onCancel} className={secondaryClass}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function destinationSummary(config: AuditShippingConfig): [string, ReactNode][] {
  if (config.type === "syslog_tls") {
    return [
      ["Destination", "Syslog over TLS"],
      ["Host", `${config.syslog.host || "Not set"}:${String(config.syslog.port)}`],
      ["CA certificate", config.syslog.ca_pem ? "Custom" : "System trust store"],
    ];
  }
  return [
    ["Destination", "Elasticsearch or OpenSearch"],
    ["Address", config.elasticsearch.url || "Not set"],
    ["Index", config.elasticsearch.index],
    [
      "Credentials",
      config.elasticsearch.auth === "api_key"
        ? `API key: ${secretLabel(config.api_key)}`
        : `${config.elasticsearch.username ?? ""}, password: ${secretLabel(config.password)}`,
    ],
    ["CA certificate", config.elasticsearch.ca_pem ? "Custom" : "System trust store"],
  ];
}

/** Settings: where Baluarte sends a copy of its audit events, and whether it is keeping up. */
export function AuditShippingCard({ csrf }: { csrf: string }) {
  const [version, setVersion] = useState(0);
  const key = `#${String(version)}`;
  const config = useLoad(`settings-audit-shipping${key}`, getAuditShipping);
  const status = useLoad(`audit-shipping-status${key}`, getShippingStatus);
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  let body: ReactNode;
  if (config.error) {
    body = <ErrorMessage error={config.error} retry={config.retry} />;
  } else if (!config.data) {
    body = (
      <p role="status" className="text-sm text-slate-600">
        Loading audit log shipping…
      </p>
    );
  } else if (editing) {
    body = (
      <ShippingForm
        config={config.data}
        csrf={csrf}
        onSaved={() => {
          setEditing(false);
          setSaved(
            "Audit log shipping saved. The worker sends new and pending events within a minute.",
          );
          setVersion((value) => value + 1);
        }}
        onCancel={() => {
          setEditing(false);
        }}
      />
    );
  } else {
    body = (
      <>
        <Values
          rows={[
            ["Shipping", config.data.enabled ? "On" : "Off"],
            ...destinationSummary(config.data),
          ]}
        />
        {status.error ? (
          <ErrorMessage error={status.error} retry={status.retry} />
        ) : status.data ? (
          <StatusPanel status={status.data} />
        ) : null}
        <button
          type="button"
          onClick={() => {
            setSaved(null);
            setEditing(true);
          }}
          className={buttonClass}
        >
          Edit audit log shipping
        </button>
      </>
    );
  }

  return (
    <section id="audit-shipping" aria-labelledby="audit-shipping-heading" className={cardClass}>
      <h2 id="audit-shipping-heading" className="text-lg font-semibold">
        Audit log shipping
      </h2>
      <p className="text-sm text-slate-600">
        Sends every audit event to a log store outside Baluarte, so a copy exists that
        Baluarte&apos;s own administrators and database cannot change. Delivery resumes where it
        stopped after an outage, and new destinations receive the full retained history.
      </p>
      <p role="status" className="text-sm text-emerald-800">
        {saved ?? ""}
      </p>
      {body}
    </section>
  );
}
