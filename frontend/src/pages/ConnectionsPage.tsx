import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import {
  addConnection,
  addGitHubConnection,
  changeConnection,
  collectConnection,
  connectionName,
  listConnections,
  providerLabels,
  removeConnection,
  sourceLabels,
  testConnection,
  type Connection,
  type Diagnostics,
  type NewConnection,
  type NewGitHubConnection,
} from "../api/connections";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { WebhookPanel } from "../connections/WebhookPanel";
import { freshnessColor, freshnessLabels } from "../freshness/status";
import { asApiError, Problems, Values } from "../settings/parts";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { Dialog } from "../ui/Dialog";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { PageHeader } from "../ui/PageHeader";

const KINDS: { kind: string; label: string }[] = [
  { kind: "capabilities", label: "Capabilities" },
  { kind: "groups", label: "Groups" },
  { kind: "projects", label: "Projects" },
];

function failure(error: unknown): string[] {
  const apiError = asApiError(error);
  return [apiError.message, ...errorReasons(apiError)];
}

function interval(minutes: number): string {
  if (minutes === 0) return "Not scheduled";
  if (minutes % 60 === 0) return `Every ${String(minutes / 60)} h`;
  return `Every ${String(minutes)} min`;
}

function useCsrf(): string {
  const { session } = useAuth();
  return session.status === "signed_in" ? session.user.csrf_token : "";
}

function NewConnectionForm({ onAdded }: { onAdded: (message: string) => void }) {
  const csrf = useCsrf();
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const text = (name: string) => {
      const entry = data.get(name);
      return typeof entry === "string" ? entry.trim() : "";
    };
    const body: NewConnection = {
      name: text("name"),
      url: text("url"),
      token: text("token"),
      timeout_seconds: Number(text("timeout_seconds") || "15"),
      sync_interval_minutes: Number(text("sync_interval_minutes") || "360"),
      allow_private_network: data.get("allow_private_network") !== null,
    };
    const found: string[] = [];
    if (!body.url.startsWith("https://")) found.push("Enter the GitLab URL, starting https://.");
    if (!body.token) found.push("Enter a token with the read_api scope.");
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const added = await addConnection(body, csrf);
      form.reset();
      onAdded(
        `${connectionName(added)} added. Test it, then collect it now or wait for its schedule.`,
      );
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="Add a GitLab connection"
      noValidate
      className="card card-body mb-3 stack-3"
    >
      <h2 className="fw-semibold">Add a GitLab connection</h2>
      <Problems messages={problems} />
      <div className="d-grid gap-3 cols-sm-2">
        <label className="fw-medium">
          Name (optional)
          <input
            name="name"
            maxLength={200}
            placeholder="EU GitLab"
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium">
          GitLab URL
          <input
            name="url"
            type="url"
            required
            placeholder="https://gitlab.example.com"
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium span-sm-2">
          Token (read_api)
          <input
            name="token"
            type="password"
            required
            autoComplete="off"
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium">
          Collect every (minutes, 0 for never)
          <input
            name="sync_interval_minutes"
            type="number"
            min={0}
            max={10080}
            defaultValue={360}
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium">
          Timeout (seconds)
          <input
            name="timeout_seconds"
            type="number"
            min={1}
            max={120}
            defaultValue={15}
            className="form-control mt-1"
          />
        </label>
        <label className="d-flex align-items-center gap-2 fw-medium span-sm-2">
          <input name="allow_private_network" type="checkbox" />
          The host is on a private network (a self-managed GitLab)
        </label>
      </div>
      <p className="small text-secondary">
        The token is sealed in the database and never shown again. Baluarte only reads from GitLab.
        Hosts that resolve to loopback, link-local or cloud metadata addresses are always refused.
      </p>
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Adding…" : "Add connection"}
      </button>
    </form>
  );
}

function NewGitHubForm({ onAdded }: { onAdded: (message: string) => void }) {
  const csrf = useCsrf();
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [kind, setKind] = useState<"token" | "app">("token");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const text = (name: string) => {
      const entry = data.get(name);
      return typeof entry === "string" ? entry.trim() : "";
    };
    const body: NewGitHubConnection = {
      name: text("name"),
      organization: text("organization"),
      auth_kind: kind,
      secret: text("secret"),
      timeout_seconds: Number(text("timeout_seconds") || "15"),
      sync_interval_minutes: Number(text("sync_interval_minutes") || "360"),
      ...(kind === "app"
        ? { app_id: Number(text("app_id")), installation_id: Number(text("installation_id")) }
        : {}),
    };
    const found: string[] = [];
    if (!body.organization) found.push("Enter the organization's name on github.com.");
    if (!body.secret)
      found.push(kind === "app" ? "Paste the App's private key." : "Enter a fine-grained token.");
    if (kind === "app" && !(body.app_id && body.installation_id))
      found.push("Enter the App ID and the installation ID.");
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const added = await addGitHubConnection(body, csrf);
      form.reset();
      setKind("token");
      onAdded(`${connectionName(added)} added. Collect it now or wait for its schedule.`);
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label="Add a GitHub organization"
      noValidate
      className="card card-body mb-3 stack-3"
    >
      <h2 className="fw-semibold">Add a GitHub organization</h2>
      <Problems messages={problems} />
      <div className="d-grid gap-3 cols-sm-2">
        <label className="fw-medium">
          Name (optional)
          <input name="name" maxLength={200} className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          Organization on github.com
          <input
            name="organization"
            required
            maxLength={39}
            placeholder="acme"
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium span-sm-2">
          Sign in with
          <select
            name="auth_kind"
            className="form-select mt-1"
            value={kind}
            onChange={(event) => setKind(event.target.value === "app" ? "app" : "token")}
          >
            <option value="token">A fine-grained personal access token</option>
            <option value="app">A GitHub App installation</option>
          </select>
        </label>
        {kind === "app" && (
          <>
            <label className="fw-medium">
              App ID
              <input name="app_id" type="number" min={1} className="form-control mt-1" />
            </label>
            <label className="fw-medium">
              Installation ID
              <input name="installation_id" type="number" min={1} className="form-control mt-1" />
            </label>
          </>
        )}
        <label className="fw-medium span-sm-2">
          {kind === "app" ? "Private key (PEM)" : "Token"}
          {kind === "app" ? (
            <textarea name="secret" rows={4} autoComplete="off" className="form-control mt-1" />
          ) : (
            <input name="secret" type="password" autoComplete="off" className="form-control mt-1" />
          )}
        </label>
        <label className="fw-medium">
          Collect every (minutes, 0 for never)
          <input
            name="sync_interval_minutes"
            type="number"
            min={0}
            max={10080}
            defaultValue={360}
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium">
          Timeout (seconds)
          <input
            name="timeout_seconds"
            type="number"
            min={1}
            max={120}
            defaultValue={15}
            className="form-control mt-1"
          />
        </label>
      </div>
      <p className="small text-secondary">
        Grant read access only: organization members and administration, and repository
        administration, metadata, pull requests, actions, deployments and issues. The credential is
        sealed in the database and never shown again. Baluarte only reads from GitHub.
      </p>
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Adding…" : "Add organization"}
      </button>
    </form>
  );
}

function DiagnosticsResult({ report }: { report: Diagnostics }) {
  const tone =
    report.status === "ok" ? "success" : report.status === "warning" ? "warning" : "danger";
  return (
    <Notice
      tone={tone}
      role="status"
      title={`Test ${report.status === "ok" ? "passed" : report.status === "warning" ? "passed with warnings" : "failed"}${report.version ? ` (GitLab ${report.version})` : ""}`}
    >
      <ul className="mb-0">
        {report.checks.map((check) => (
          <li key={check.id}>{check.summary}</li>
        ))}
      </ul>
    </Notice>
  );
}

function ConnectionCard({
  connection,
  manage,
  onChanged,
}: {
  connection: Connection;
  manage: boolean;
  onChanged: (message: string) => void;
}) {
  const csrf = useCsrf();
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<Diagnostics | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const title = connectionName(connection);

  async function act(run: () => Promise<string | null>) {
    setBusy(true);
    setProblems([]);
    try {
      const message = await run();
      if (message) onChanged(message);
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label={title} className="card card-body mb-3 stack-3">
      <div className="d-flex flex-wrap align-items-center gap-2">
        <h2 className="fw-semibold mb-0">{title}</h2>
        <Badge tone={freshnessColor[connection.health]}>{freshnessLabels[connection.health]}</Badge>
        <Badge tone="secondary">{providerLabels[connection.provider] ?? connection.provider}</Badge>
        {connection.primary && <Badge tone="info">Primary</Badge>}
        {connection.paused && <Badge tone="warning">Paused</Badge>}
      </div>
      <Problems messages={problems} />
      <Values
        rows={[
          ["URL", <span className="text-break">{connection.url}</span>],
          ["Settings", sourceLabels[connection.source]],
          [
            connection.auth_kind === "app" ? "GitHub App" : "Token",
            connection.auth_kind === "app"
              ? `App ${String(connection.app_id)}, installation ${String(connection.installation_id)}`
              : connection.token === "configured"
                ? "Stored"
                : "Not set",
          ],
          ["Collection", connection.paused ? "Paused" : interval(connection.sync_interval_minutes)],
          [
            providerLabels[connection.provider] ?? "Provider",
            connection.version
              ? `${connection.version}${connection.edition ? ` (${connection.edition})` : ""}`
              : "Not detected yet",
          ],
          ...(connection.allow_private_network
            ? ([["Network", "Private network allowed"]] as [string, string][])
            : []),
        ]}
      />
      <dl className="row g-2 mb-1">
        {KINDS.map(({ kind, label }) => {
          const entry = connection.collections[kind];
          if (!entry) return null;
          return (
            <div key={kind} className="col-sm-4">
              <div className="border rounded p-2 h-100">
                <dt className="fw-medium">{label}</dt>
                <dd>
                  {freshnessLabels[entry.status]}
                  {entry.running && " (collecting now)"}
                </dd>
                <dd className="text-secondary">
                  Last success: {formatTime(entry.last_success_at)}
                </dd>
                {entry.last_run?.error && entry.last_run.status !== "succeeded" && (
                  <dd className="mt-1 text-break">{entry.last_run.error}</dd>
                )}
              </div>
            </div>
          );
        })}
      </dl>
      {report && <DiagnosticsResult report={report} />}
      {manage && connection.provider === "github" && (
        <WebhookPanel connectionId={connection.id} csrf={csrf} />
      )}
      {manage && (
        <div className="d-flex flex-wrap gap-2">
          {connection.provider === "gitlab" && (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  setReport(await testConnection(connection.id, csrf));
                  return null;
                })
              }
            >
              Test connection
            </button>
          )}
          <button
            type="button"
            className="btn"
            disabled={busy || connection.paused}
            onClick={() =>
              void act(async () => {
                await collectConnection(connection.id, csrf);
                return `Collection of ${title} queued.`;
              })
            }
          >
            Collect now
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                await changeConnection(connection.id, { paused: !connection.paused }, csrf);
                return connection.paused ? `${title} resumed.` : `${title} paused.`;
              })
            }
          >
            {connection.paused ? "Resume" : "Pause"}
          </button>
          {!connection.primary && (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => setConfirmRemove(true)}
            >
              Remove
            </button>
          )}
        </div>
      )}
      {connection.primary && manage && (
        <p className="small text-secondary mb-0">
          This connection&apos;s URL, token and schedule are changed on the{" "}
          <Link to="/settings">Settings page</Link>
          {connection.source === "deployment" ? " or in the deployment" : ""}.
        </p>
      )}
      <Dialog
        open={confirmRemove}
        title={`Remove ${title}?`}
        onClose={() => setConfirmRemove(false)}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirmRemove(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  await removeConnection(connection.id, csrf);
                  setConfirmRemove(false);
                  return `${title} removed. Its inventory and evidence are kept.`;
                })
              }
            >
              Remove connection
            </button>
          </>
        }
      >
        <p>
          Baluarte stops collecting {connection.url} and forgets its token. Its inventory, findings
          and evidence stay. Adding the same URL again restores it.
        </p>
      </Dialog>
    </section>
  );
}

export function ConnectionsPage() {
  const { session } = useAuth();
  const manage =
    session.status === "signed_in" && session.user.permissions.includes("settings:manage");
  const [message, setMessage] = useState<string | null>(null);
  const loaded = useLoad("connections", listConnections);

  function changed(text: string) {
    setMessage(text);
    loaded.retry();
  }

  return (
    <div className="stack-3">
      <PageHeader
        pretitle="Admin"
        title="Connections"
        subtitle="The GitLab deployments and GitHub organizations Baluarte collects from, each with its own credential, schedule and health."
      />
      {message && (
        <Notice tone="success" role="status">
          {message}
        </Notice>
      )}
      {loaded.loading && <Loading label="Loading connections…" />}
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {loaded.data?.connections.length === 0 && (
        <EmptyState title="No connections yet">
          Set the primary GitLab connection on the Settings page, or add one below.
        </EmptyState>
      )}
      {loaded.data?.connections.map((connection) => (
        <ConnectionCard
          key={connection.id}
          connection={connection}
          manage={manage}
          onChanged={changed}
        />
      ))}
      {manage && <NewConnectionForm onAdded={changed} />}
      {manage && <NewGitHubForm onAdded={changed} />}
    </div>
  );
}
