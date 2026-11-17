import { useId, useState, type FormEvent, type ReactNode } from "react";

import {
  CHANNELS,
  getDeliveries,
  getNotifications,
  MAX_RECIPIENTS,
  saveNotifications,
  SEVERITIES,
  testNotifications,
  type Channel,
  type ChannelTestResult,
  type Delivery,
  type DeliveryStatus,
  type EventName,
  type NotificationEvents,
  type NotificationsConfig,
  type NotificationsUpdate,
  type Security,
  type Severity,
} from "../api/notifications";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { fieldClass } from "../components/styles";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";
import { asApiError, cardClass, errorClass, Problems, secretLabel, Values } from "./parts";

export const CHANNEL_LABEL: Record<Channel, string> = {
  email: "Email",
  webhook: "Chat webhook",
};

export const EVENT_LABEL: Record<EventName, string> = {
  new_findings: "New findings",
  collection_health: "Collection unhealthy or recovered",
  review_waiting: "Waiting for review (daily)",
  weekly_digest: "Weekly digest (Mondays)",
};

const EVENT_HINT: Record<EventName, string> = {
  new_findings: "After each evaluation: findings it opened, up to 10 listed with the total.",
  collection_health: "Once when GitLab collection becomes unhealthy, once when it recovers.",
  review_waiting:
    "Once a day from 08:00 UTC: items waiting longer than the days below, and exceptions and plans expiring within 7 days.",
  weekly_digest:
    "Mondays from 08:00 UTC: rule results, open findings, controls, the review queue and what expires next week.",
};

const EVENTS: readonly EventName[] = [
  "new_findings",
  "collection_health",
  "review_waiting",
  "weekly_digest",
];

const STATUS: Record<DeliveryStatus, { label: string; tone: Tone }> = {
  pending: { label: "Pending", tone: "secondary" },
  sent: { label: "Sent", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

const EVENT_TYPE_LABEL: Record<string, string> = {
  new_findings: "New findings",
  collection_unhealthy: "Collection unhealthy",
  collection_recovered: "Collection recovered",
  review_waiting: "Waiting for review",
  weekly_digest: "Weekly digest",
};

export interface Draft {
  emailEnabled: boolean;
  host: string;
  port: string;
  security: Security;
  username: string;
  password: string;
  fromAddress: string;
  recipients: string;
  emailCa: string;
  webhookEnabled: boolean;
  webhookUrl: string;
  webhookCa: string;
  events: NotificationEvents;
}

export function draftFrom(config: NotificationsConfig): Draft {
  return {
    emailEnabled: config.email.enabled,
    host: config.email.host,
    port: String(config.email.port),
    security: config.email.security,
    username: config.email.username ?? "",
    password: "",
    fromAddress: config.email.from_address,
    recipients: config.email.recipients.join("\n"),
    emailCa: config.email.ca_pem ?? "",
    webhookEnabled: config.webhook.enabled,
    webhookUrl: "",
    webhookCa: config.webhook.ca_pem ?? "",
    events: structuredClone(config.events),
  };
}

function orNull(value: string): string | null {
  return value.trim() ? value.trim() : null;
}

export function parseRecipients(text: string): string[] {
  const seen = new Set<string>();
  return text
    .split(/[\n,;]/)
    .map((address) => address.trim())
    .filter((address) => {
      const key = address.toLowerCase();
      if (!address || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function toUpdate(draft: Draft): NotificationsUpdate {
  const username = orNull(draft.username);
  return {
    email: {
      enabled: draft.emailEnabled,
      host: draft.host.trim(),
      port: Number(draft.port),
      security: draft.security,
      username,
      // Blank keeps the stored password; without a username none is kept.
      password: username === null ? null : orNull(draft.password),
      from_address: draft.fromAddress.trim(),
      recipients: parseRecipients(draft.recipients),
      ca_pem: orNull(draft.emailCa),
    },
    // Blank keeps the stored URL.
    webhook: {
      enabled: draft.webhookEnabled,
      url: orNull(draft.webhookUrl),
      ca_pem: orNull(draft.webhookCa),
    },
    events: draft.events,
  };
}

const ADDRESS = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Problems with the email channel, named by field; checked for a test and an enabled save. */
export function emailProblems(draft: Draft, stored: NotificationsConfig): string[] {
  const problems: string[] = [];
  if (!draft.host.trim()) problems.push("SMTP host: enter the mail server's host name.");
  else if (draft.host.includes("://"))
    problems.push("SMTP host: enter a host name without a scheme.");
  const port = Number(draft.port);
  if (!/^\d+$/.test(draft.port.trim()) || port < 1 || port > 65535) {
    problems.push("Port: enter a port from 1 to 65535.");
  }
  if (!ADDRESS.test(draft.fromAddress.trim())) problems.push("From: enter an email address.");
  const recipients = parseRecipients(draft.recipients);
  if (recipients.length === 0) problems.push("Recipients: enter at least one address.");
  if (recipients.length > MAX_RECIPIENTS) {
    problems.push(`Recipients: at most ${String(MAX_RECIPIENTS)} addresses.`);
  }
  const bad = recipients.find((address) => !ADDRESS.test(address));
  if (bad) problems.push(`Recipients: ${bad} is not an email address.`);
  const username = draft.username.trim();
  if (username && !draft.password.trim()) {
    const moved =
      draft.host.trim() !== stored.email.host || username !== (stored.email.username ?? "");
    if (stored.email.password === "not_set" || moved) {
      problems.push(
        moved && stored.email.password === "configured"
          ? "Password: enter it again, because the host or username changed."
          : "Password: enter the password for this username.",
      );
    }
  }
  return problems;
}

/** Problems with the webhook channel; checked for a test and an enabled save. */
export function webhookProblems(draft: Draft, stored: NotificationsConfig): string[] {
  const url = draft.webhookUrl.trim();
  if (!url) {
    return stored.webhook.url === "configured" ? [] : ["Webhook URL: enter the URL."];
  }
  return /^https:\/\/[^\s/]+\/?\S*$/i.test(url)
    ? []
    : ["Webhook URL: enter an https:// URL. Messages are never sent unencrypted."];
}

export function notificationProblems(draft: Draft, stored: NotificationsConfig): string[] {
  const problems = [
    ...(draft.emailEnabled ? emailProblems(draft, stored) : []),
    ...(draft.webhookEnabled ? webhookProblems(draft, stored) : []),
  ];
  const days = draft.events.review_waiting.older_than_days;
  if (!Number.isInteger(days) || days < 1 || days > 30) {
    problems.push("Waiting for review: choose 1 to 30 days.");
  }
  return problems;
}

function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  children: (props: { id: string; "aria-describedby"?: string }) => ReactNode;
}) {
  return (
    <div className="stack-1">
      <label htmlFor={id} className="d-block fw-medium">
        {label}
      </label>
      {children({ id, ...(hint ? { "aria-describedby": `${id}-hint` } : {}) })}
      {hint && (
        <p id={`${id}-hint`} className="small text-secondary">
          {hint}
        </p>
      )}
    </div>
  );
}

function TestOutcome({ result }: { result: ChannelTestResult }) {
  return (
    <p
      role={result.ok ? "status" : "alert"}
      className={result.ok ? "alert alert-success text-body" : errorClass}
    >
      {result.ok ? `Test sent. ${result.detail}` : `Test failed. ${result.detail}`}
    </p>
  );
}

function NotificationsForm({
  config,
  csrf,
  onSaved,
  onCancel,
}: {
  config: NotificationsConfig;
  csrf: string;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const { refresh } = useAuth();
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => draftFrom(config));
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState<"save" | Channel | null>(null);
  const [tests, setTests] = useState<Partial<Record<Channel, ChannelTestResult>>>({});

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  function setEvent<E extends EventName>(event: E, change: Partial<NotificationEvents[E]>) {
    setDraft((current) => ({
      ...current,
      events: {
        ...current.events,
        [event]: { ...current.events[event], ...change },
      },
    }));
  }

  function failed(error: unknown) {
    const apiError = asApiError(error);
    setProblems([apiError.message]);
    if (apiError.status === 401) void refresh();
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const found = notificationProblems(draft, config);
    setProblems(found);
    if (found.length > 0) return;
    setBusy("save");
    try {
      await saveNotifications(toUpdate(draft), csrf);
      onSaved();
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
    }
  }

  async function sendTest(channel: Channel) {
    setTests((current) => ({ ...current, [channel]: undefined }));
    const found =
      channel === "email" ? emailProblems(draft, config) : webhookProblems(draft, config);
    setProblems(found);
    if (found.length > 0) return;
    setBusy(channel);
    try {
      const result = await testNotifications(toUpdate(draft), channel, csrf);
      setTests((current) => ({ ...current, [channel]: result }));
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
    }
  }

  const text = (
    key: "host" | "port" | "username" | "fromAddress",
    label: string,
    hint?: string,
    inputMode?: "numeric" | "email",
  ) => (
    <Field id={`${id}-${key}`} label={label} {...(hint ? { hint } : {})}>
      {(props) => (
        <input
          {...props}
          type="text"
          inputMode={inputMode}
          value={draft[key]}
          onChange={(event) => {
            set(key, event.currentTarget.value);
          }}
          className="form-control mt-1"
        />
      )}
    </Field>
  );

  const secret = (
    key: "password" | "webhookUrl",
    label: string,
    stored: "configured" | "not_set",
  ) => (
    <Field
      id={`${id}-${key}`}
      label={label}
      hint={
        stored === "configured"
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
          className="form-control mt-1"
        />
      )}
    </Field>
  );

  const ca = (key: "emailCa" | "webhookCa") => (
    <Field
      id={`${id}-${key}`}
      label="CA certificate (PEM, optional)"
      hint="Only for a private certificate authority. The server's certificate is always verified."
    >
      {(props) => (
        <textarea
          {...props}
          value={draft[key]}
          rows={3}
          spellCheck={false}
          onChange={(event) => {
            set(key, event.currentTarget.value);
          }}
          className={`${fieldClass} font-monospace small`}
        />
      )}
    </Field>
  );

  const testButton = (channel: Channel) => (
    <button
      type="button"
      disabled={busy !== null}
      onClick={() => void sendTest(channel)}
      className="btn"
    >
      {busy === channel ? "Sending…" : `Send test ${channel === "email" ? "email" : "to webhook"}`}
    </button>
  );

  return (
    <form className="stack-4" onSubmit={(event) => void save(event)} noValidate>
      <Problems messages={problems} />
      <fieldset className="stack-3">
        <legend className="fw-medium fs-3">Email</legend>
        <label className="d-flex align-items-center gap-2 fw-medium">
          <input
            type="checkbox"
            checked={draft.emailEnabled}
            onChange={(event) => {
              set("emailEnabled", event.currentTarget.checked);
            }}
            className="h-4 w-4"
          />
          Send email
        </label>
        <div className="d-grid gap-3 cols-sm-2">
          {text("host", "SMTP host")}
          {text("port", "Port", "587 for STARTTLS, 465 for TLS.", "numeric")}
        </div>
        <fieldset className="stack-2">
          <legend className="fw-medium">Security</legend>
          {(
            [
              ["starttls", "STARTTLS (upgrade the connection before anything is sent)"],
              ["tls", "TLS from the start (implicit TLS)"],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className="d-flex align-items-center gap-2">
              <input
                type="radio"
                name={`${id}-security`}
                value={value}
                checked={draft.security === value}
                onChange={() => {
                  set("security", value);
                }}
                className="h-4 w-4"
              />
              {label}
            </label>
          ))}
        </fieldset>
        <div className="d-grid gap-3 cols-sm-2">
          {text("username", "Username (optional)")}
          {draft.username.trim() ? secret("password", "Password", config.email.password) : null}
        </div>
        {text("fromAddress", "From", "The address messages come from.", "email")}
        <Field
          id={`${id}-recipients`}
          label="Recipients"
          hint={`One address per line, at most ${String(MAX_RECIPIENTS)}. A team list works best.`}
        >
          {(props) => (
            <textarea
              {...props}
              value={draft.recipients}
              rows={3}
              spellCheck={false}
              onChange={(event) => {
                set("recipients", event.currentTarget.value);
              }}
              className={fieldClass}
            />
          )}
        </Field>
        {ca("emailCa")}
        {tests.email && <TestOutcome result={tests.email} />}
        <div>{testButton("email")}</div>
      </fieldset>
      <fieldset className="stack-3">
        <legend className="fw-medium fs-3">Chat webhook</legend>
        <label className="d-flex align-items-center gap-2 fw-medium">
          <input
            type="checkbox"
            checked={draft.webhookEnabled}
            onChange={(event) => {
              set("webhookEnabled", event.currentTarget.checked);
            }}
            className="h-4 w-4"
          />
          Post to a chat webhook
        </label>
        <p className="small text-secondary">
          An incoming webhook URL from Slack, Mattermost or another tool that accepts Slack-style
          JSON. The URL holds a token, so it is stored encrypted and never shown.
          {config.webhook.host ? ` The stored URL points at ${config.webhook.host}.` : ""}
        </p>
        {secret("webhookUrl", "Webhook URL", config.webhook.url)}
        {ca("webhookCa")}
        {tests.webhook && <TestOutcome result={tests.webhook} />}
        <div>{testButton("webhook")}</div>
      </fieldset>
      <fieldset className="stack-3">
        <legend className="fw-medium fs-3">Events</legend>
        {EVENTS.map((event) => {
          const settings = draft.events[event];
          return (
            <div
              key={event}
              role="group"
              aria-label={EVENT_LABEL[event]}
              className="stack-2 border rounded p-3"
            >
              <label className="d-flex align-items-center gap-2 fw-medium">
                <input
                  type="checkbox"
                  checked={settings.enabled}
                  onChange={(change) => {
                    setEvent(event, { enabled: change.currentTarget.checked });
                  }}
                  className="h-4 w-4"
                />
                {EVENT_LABEL[event]}
              </label>
              <p className="small text-secondary">{EVENT_HINT[event]}</p>
              <div className="d-flex flex-wrap gap-3">
                {CHANNELS.map((channel) => (
                  <label key={channel} className="d-flex align-items-center gap-2">
                    <input
                      type="checkbox"
                      checked={settings.channels.includes(channel)}
                      onChange={(change) => {
                        const on = change.currentTarget.checked;
                        setEvent(event, {
                          channels: CHANNELS.filter((c) =>
                            c === channel ? on : settings.channels.includes(c),
                          ),
                        });
                      }}
                      aria-label={`${CHANNEL_LABEL[channel]} for ${EVENT_LABEL[event]}`}
                      className="h-4 w-4"
                    />
                    {CHANNEL_LABEL[channel]}
                  </label>
                ))}
              </div>
              {event === "new_findings" && (
                <Field id={`${id}-severity`} label="At or above severity">
                  {(props) => (
                    <select
                      {...props}
                      value={draft.events.new_findings.min_severity}
                      onChange={(change) => {
                        setEvent("new_findings", {
                          min_severity: change.currentTarget.value as Severity,
                        });
                      }}
                      className="form-select mt-1 w-auto"
                    >
                      {SEVERITIES.map((severity) => (
                        <option key={severity} value={severity}>
                          {severity}
                        </option>
                      ))}
                    </select>
                  )}
                </Field>
              )}
              {event === "review_waiting" && (
                <Field id={`${id}-days`} label="Waiting longer than (days)" hint="1 to 30 days.">
                  {(props) => (
                    <input
                      {...props}
                      type="number"
                      min={1}
                      max={30}
                      step={1}
                      value={String(draft.events.review_waiting.older_than_days)}
                      onChange={(change) => {
                        setEvent("review_waiting", {
                          older_than_days: Number(change.currentTarget.value),
                        });
                      }}
                      className="form-control mt-1 w-auto"
                    />
                  )}
                </Field>
              )}
            </div>
          );
        })}
      </fieldset>
      <div className="d-flex flex-wrap gap-2">
        <button type="submit" disabled={busy !== null} className="btn btn-primary">
          {busy === "save" ? "Saving…" : "Save notifications"}
        </button>
        <button type="button" onClick={onCancel} className="btn">
          Cancel
        </button>
      </div>
    </form>
  );
}

function eventSummary(config: NotificationsConfig, event: EventName): string {
  const settings = config.events[event];
  if (!settings.enabled || settings.channels.length === 0) return "Off";
  const to = settings.channels.map((channel) => CHANNEL_LABEL[channel]).join(" and ");
  if (event === "new_findings")
    return `${to}, ${config.events.new_findings.min_severity} and above`;
  if (event === "review_waiting") {
    return `${to}, waiting more than ${String(config.events.review_waiting.older_than_days)} days`;
  }
  return to;
}

function Summary({ config }: { config: NotificationsConfig }) {
  const email = config.email;
  return (
    <Values
      rows={[
        [
          "Email",
          email.enabled
            ? `On: ${email.host}:${String(email.port)} (${email.security === "tls" ? "TLS" : "STARTTLS"}), ${String(email.recipients.length)} recipients`
            : "Off",
        ],
        ...(email.username
          ? ([["SMTP password", secretLabel(email.password)]] as [string, ReactNode][])
          : []),
        [
          "Chat webhook",
          config.webhook.enabled
            ? `On: ${config.webhook.host ?? "stored URL"}`
            : config.webhook.url === "configured"
              ? "Off (URL stored)"
              : "Off",
        ],
        ...EVENTS.map((event): [string, ReactNode] => [
          EVENT_LABEL[event],
          eventSummary(config, event),
        ]),
        [
          "Links",
          config.public_url ?? (
            <span className="text-secondary">
              Left out: Baluarte&apos;s address is not configured
            </span>
          ),
        ],
      ]}
    />
  );
}

const deliveryColumns: readonly Column<Delivery>[] = [
  {
    key: "event",
    header: "Event",
    rowHeader: true,
    render: (row) => EVENT_TYPE_LABEL[row.event_type] ?? row.event_type,
  },
  {
    key: "channel",
    header: "Channel",
    render: (row) => CHANNEL_LABEL[row.channel],
  },
  {
    key: "status",
    header: "Status",
    render: (row) => <Badge tone={STATUS[row.status].tone}>{STATUS[row.status].label}</Badge>,
  },
  {
    key: "time",
    header: "Time",
    render: (row) =>
      row.status === "sent"
        ? `Sent ${formatTime(row.sent_at)}`
        : row.status === "pending" && row.next_attempt_at
          ? `Next attempt ${formatTime(row.next_attempt_at)}`
          : `Created ${formatTime(row.created_at)}`,
    priority: 2,
  },
  {
    key: "attempts",
    header: "Attempts",
    render: (row) => row.attempts,
    numeric: true,
    priority: 2,
  },
  {
    key: "error",
    header: "Last error",
    render: (row) => row.last_error ?? <span className="text-secondary">None</span>,
  },
];

function Deliveries({ version }: { version: number }) {
  const loaded = useLoad(`notification-deliveries#${String(version)}`, getDeliveries);
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) {
    return (
      <p role="status" className="text-secondary">
        Loading recent deliveries…
      </p>
    );
  }
  if (loaded.data.length === 0) {
    return <p className="text-secondary">Nothing has been sent yet.</p>;
  }
  return (
    <ResponsiveTable
      caption="Recent notification deliveries"
      showCaption
      columns={deliveryColumns}
      rows={loaded.data}
      rowKey={(row) => row.id}
    />
  );
}

/** Settings: email and chat notifications for what people must act on, and what was sent. */
export function NotificationsCard({ csrf }: { csrf: string }) {
  const [version, setVersion] = useState(0);
  const config = useLoad(`settings-notifications#${String(version)}`, getNotifications);
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  let body: ReactNode;
  if (config.error) {
    body = <ErrorMessage error={config.error} retry={config.retry} />;
  } else if (!config.data) {
    body = (
      <p role="status" className="text-secondary">
        Loading notifications…
      </p>
    );
  } else if (editing) {
    body = (
      <NotificationsForm
        config={config.data}
        csrf={csrf}
        onSaved={() => {
          setEditing(false);
          setSaved("Notifications saved. Every change is audited.");
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
        <Summary config={config.data} />
        <div>
          <button
            type="button"
            onClick={() => {
              setSaved(null);
              setEditing(true);
            }}
            className="btn btn-primary"
          >
            Edit notifications
          </button>
        </div>
      </>
    );
  }

  return (
    <section id="notifications" aria-labelledby="notifications-heading" className={cardClass}>
      <h2 id="notifications-heading" className="card-title">
        Notifications
      </h2>
      <p className="text-secondary">
        Sends what people must act on to email and a chat webhook, once each: new findings,
        collection health, items waiting for review and a weekly digest. Off until a channel is
        turned on; nothing leaves Baluarte otherwise. Messages hold counts, project paths, rule IDs
        and links, never people&apos;s names or addresses.
      </p>
      <p role="status" className="text-success-emphasis">
        {saved ?? ""}
      </p>
      <div className="stack-3">{body}</div>
      <div className="stack-2 mt-3">
        <h3 className="fs-3">Recent deliveries</h3>
        <Deliveries version={version} />
      </div>
    </section>
  );
}
