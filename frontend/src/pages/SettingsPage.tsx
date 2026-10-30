import { useState, type ReactNode } from "react";

import {
  disconnectGitLab,
  getGitLabConfig,
  getOidcConfig,
  getSettings,
  SETTINGS,
  type GitLabConfig,
  type OidcConfig,
  type SettingsInfo,
} from "../api/setup";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, secondaryClass } from "../components/styles";
import { describeInterval } from "../settings/format";
import { GitLabForm } from "../settings/GitLabForm";
import { OidcForm } from "../settings/OidcForm";
import {
  asApiError,
  cardClass,
  DeploymentNotice,
  errorClass,
  groupList,
  secretLabel,
  Values,
} from "../settings/parts";

function Loading({ what }: { what: string }) {
  return (
    <p role="status" className="text-sm text-slate-600">
      Loading {what}…
    </p>
  );
}

function Card({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className={cardClass}>
      <h2 id={id} className="text-lg font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Saved({ message }: { message: string | null }) {
  return (
    <p role="status" className="text-sm text-emerald-800">
      {message ?? ""}
    </p>
  );
}

function GitLabCard({ csrf, secretKey }: { csrf: string; secretKey: boolean | null }) {
  const { refresh } = useAuth();
  const [version, setVersion] = useState(0);
  const loaded = useLoad(`settings-gitlab#${String(version)}`, (signal) =>
    getGitLabConfig(SETTINGS, signal),
  );
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  async function disconnect() {
    setFailure(null);
    try {
      await disconnectGitLab(csrf);
      setConfirming(false);
      setSaved("GitLab was disconnected. Collected data is kept.");
      setVersion((value) => value + 1);
    } catch (error) {
      const apiError = asApiError(error);
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    }
  }

  let body: ReactNode;
  if (loaded.error) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (loaded.loading || loaded.data === undefined) {
    body = <Loading what="the GitLab connection" />;
  } else {
    const config: GitLabConfig | null = loaded.data;
    const deployment = config?.source === "deployment";
    if (editing && !deployment) {
      body = (
        <GitLabForm
          base={SETTINGS}
          csrf={csrf}
          config={config}
          secretKeyConfigured={secretKey}
          onSaved={() => {
            setEditing(false);
            setSaved("GitLab connection saved. The next collection uses it.");
            setVersion((value) => value + 1);
          }}
          onUnauthorized={() => void refresh()}
          onCancel={() => {
            setEditing(false);
          }}
        />
      );
    } else {
      body = (
        <>
          {deployment && <DeploymentNotice what="The GitLab connection" />}
          {config ? (
            <Values
              rows={[
                ["GitLab address", config.url],
                ["Access token", secretLabel(config.token)],
                ["Request timeout", `${String(config.timeout_seconds)} seconds`],
                ["Collection", describeInterval(config.sync_interval_minutes)],
              ]}
            />
          ) : (
            <p className="text-sm text-slate-700">
              GitLab is not connected, so nothing is collected or evaluated.
            </p>
          )}
          {failure && (
            <p role="alert" className={errorClass}>
              {failure}
            </p>
          )}
          {!deployment && (
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => {
                  setSaved(null);
                  setEditing(true);
                }}
                className={buttonClass}
              >
                {config ? "Edit connection" : "Connect GitLab"}
              </button>
              {config && !confirming && (
                <button
                  type="button"
                  onClick={() => {
                    setConfirming(true);
                  }}
                  className={secondaryClass}
                >
                  Disconnect
                </button>
              )}
            </div>
          )}
          {confirming && (
            <div
              role="group"
              aria-label="Confirm disconnect"
              className="space-y-2 rounded border border-red-200 p-3"
            >
              <p className="text-sm">
                Disconnect GitLab? The stored token is deleted and collection stops. Collected
                inventory, evidence and findings are kept.
              </p>
              <div className="flex gap-2">
                <button type="button" onClick={() => void disconnect()} className={buttonClass}>
                  Disconnect GitLab
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setConfirming(false);
                  }}
                  className={secondaryClass}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </>
      );
    }
  }

  return (
    <Card id="gitlab-heading" title="GitLab connection">
      <Saved message={saved} />
      {body}
    </Card>
  );
}

function OidcCard({ csrf, secretKey }: { csrf: string; secretKey: boolean | null }) {
  const { refresh } = useAuth();
  const [version, setVersion] = useState(0);
  const loaded = useLoad(`settings-oidc#${String(version)}`, (signal) =>
    getOidcConfig(SETTINGS, signal),
  );
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  let body: ReactNode;
  if (loaded.error) {
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  } else if (loaded.loading || loaded.data === undefined) {
    body = <Loading what="the identity provider" />;
  } else {
    const config: OidcConfig | null = loaded.data;
    const deployment = config?.source === "deployment";
    if (editing && !deployment) {
      body = (
        <OidcForm
          base={SETTINGS}
          csrf={csrf}
          config={config}
          secretKeyConfigured={secretKey}
          lockoutWarning
          onSaved={() => {
            setEditing(false);
            setSaved(
              "Identity provider saved. New sign-ins use it; current sessions keep their roles until they end.",
            );
            setVersion((value) => value + 1);
          }}
          onUnauthorized={() => void refresh()}
          onCancel={() => {
            setEditing(false);
          }}
        />
      );
    } else {
      body = (
        <>
          {deployment && <DeploymentNotice what="Sign-in" />}
          {config && (
            <Values
              rows={[
                ["Baluarte address", config.public_url],
                ["Redirect URI", config.redirect_uri],
                ["Issuer", config.issuer],
                ["Client ID", config.client_id],
                ["Client secret", secretLabel(config.client_secret)],
                ["Scopes", config.scopes],
                ["Roles claim", config.roles_claim],
                ["Admin groups", groupList(config.admin_groups)],
                ["Analyst groups", groupList(config.analyst_groups)],
                ["Auditor groups", groupList(config.auditor_groups)],
              ]}
            />
          )}
          {!deployment && (
            <button
              type="button"
              onClick={() => {
                setSaved(null);
                setEditing(true);
              }}
              className={buttonClass}
            >
              Edit identity provider
            </button>
          )}
        </>
      );
    }
  }

  return (
    <Card id="oidc-heading" title="Identity provider">
      <Saved message={saved} />
      {body}
    </Card>
  );
}

function SystemCard({ info }: { info: SettingsInfo }) {
  const database = info.database;
  return (
    <Card id="system-heading" title="System">
      <Values
        rows={[
          ["Version", info.version],
          [
            "Database",
            database
              ? `${database.user ?? "?"}@${database.host ?? "?"}:${String(database.port ?? 5432)}/${database.name ?? "?"}${database.ssl_mode ? ` (sslmode ${database.ssl_mode})` : ""}`
              : "Not configured",
          ],
          ["Database password", database ? secretLabel(database.password) : "Not set"],
          [
            "Logging",
            info.logging
              ? `${info.logging.level}, ${info.logging.format}`
              : "Invalid; using INFO, json",
          ],
          [
            "Secret encryption key",
            info.secret_key === undefined ? "Unknown" : secretLabel(info.secret_key),
          ],
          [
            "Session lifetime",
            `${String(info.authentication.session_idle_minutes)} minutes idle, ${String(info.authentication.session_lifetime_minutes)} minutes at most`,
          ],
        ]}
      />
      <p className="text-xs text-slate-600">
        These come from the deployment and change only there.
      </p>
    </Card>
  );
}

export function SettingsPage() {
  const { session } = useAuth();
  const allowed =
    session.status === "signed_in" && session.user.permissions.includes("settings:manage");

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Settings
        </h1>
        <p className="mt-2 text-slate-600">
          The identity provider, the GitLab connection and system information. Every change is
          audited; secrets can be replaced but are never shown.
        </p>
      </div>
      {allowed ? (
        <SettingsContent csrf={session.user.csrf_token} />
      ) : (
        <p role="alert" className={errorClass}>
          Only administrators can view and change settings.
        </p>
      )}
    </section>
  );
}

function SettingsContent({ csrf }: { csrf: string }) {
  const info = useLoad("settings", getSettings);
  const secretKey =
    info.data?.secret_key === undefined ? null : info.data.secret_key === "configured";

  return (
    <>
      <GitLabCard csrf={csrf} secretKey={secretKey} />
      <OidcCard csrf={csrf} secretKey={secretKey} />
      {info.error ? (
        <ErrorMessage error={info.error} retry={info.retry} />
      ) : info.data ? (
        <SystemCard info={info.data} />
      ) : (
        <Loading what="system information" />
      )}
    </>
  );
}
