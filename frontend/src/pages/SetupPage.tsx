import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import {
  getGitLabConfig,
  getOidcConfig,
  getSetupStatus,
  SETUP,
  startSetupSession,
  type GitLabConfig,
  type OidcConfig,
  type SetupStatus,
} from "../api/setup";
import { useAuth } from "../auth/context";
import { signInHref } from "../auth/session";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { describeInterval } from "../settings/format";
import { GitLabForm } from "../settings/GitLabForm";
import { OidcForm } from "../settings/OidcForm";
import {
  asApiError,
  cardClass,
  DeploymentNotice,
  groupList,
  noteClass,
  Problems,
  secretLabel,
  SecretKeyNotice,
  Values,
} from "../settings/parts";

type Step = "oidc" | "gitlab" | "review";

const steps: { step: Step; label: string }[] = [
  { step: "oidc", label: "Identity provider" },
  { step: "gitlab", label: "GitLab" },
  { step: "review", label: "Review and finish" },
];

// The setup session's CSRF token lives for this tab only, so a reload keeps the wizard open.
const CSRF_KEY = "baluarte.setup.csrf";

function readCsrf(): string | null {
  try {
    return window.sessionStorage.getItem(CSRF_KEY);
  } catch {
    return null;
  }
}

function storeCsrf(token: string | null) {
  try {
    if (token) window.sessionStorage.setItem(CSRF_KEY, token);
    else window.sessionStorage.removeItem(CSRF_KEY);
  } catch {
    // Storage blocked: the token is kept in memory for this page only.
  }
}

const primaryLinkClass = `inline-block ${buttonClass}`;

function SignInToFinish() {
  return (
    <a href={signInHref("/settings")} className={primaryLinkClass}>
      Sign in to finish setup
    </a>
  );
}

function CodeStep({
  awaiting,
  notice,
  onAccepted,
}: {
  awaiting: boolean;
  notice: string | null;
  onAccepted: (csrf: string) => void;
}) {
  const [code, setCode] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [sending, setSending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!code.trim()) {
      setProblems(["Enter the setup code."]);
      return;
    }
    setProblems([]);
    setSending(true);
    try {
      onAccepted(await startSetupSession(code.trim()));
    } catch (error) {
      setProblems([asApiError(error).message]);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="space-y-6">
      {notice && (
        <p role="status" className={noteClass}>
          {notice}
        </p>
      )}
      {awaiting && (
        <div className={cardClass}>
          <h2 className="text-lg font-semibold">Finish setup by signing in</h2>
          <p className="text-sm text-slate-700">
            The identity provider is configured. Setup finishes, and this page locks, when someone
            in an admin group signs in. To change settings before then, enter the setup code below.
          </p>
          <SignInToFinish />
        </div>
      )}
      <form
        onSubmit={(event) => void submit(event)}
        aria-labelledby="setup-code-heading"
        noValidate
        className={cardClass}
      >
        <h2 id="setup-code-heading" className="text-lg font-semibold">
          Enter the setup code
        </h2>
        <p className="text-sm text-slate-700">
          Baluarte prints a one-time setup code in the API&apos;s log each time it starts until
          setup is complete. Only someone who can read the server&apos;s logs can install it.
        </p>
        <Problems messages={problems} />
        <label className="block text-sm font-medium">
          Setup code
          <input
            name="code"
            value={code}
            onChange={(event) => {
              setCode(event.currentTarget.value);
            }}
            autoComplete="one-time-code"
            spellCheck={false}
            className={`${fieldClass} font-mono`}
          />
        </label>
        <details className="text-sm">
          <summary className="cursor-pointer font-medium">Where to find the code</summary>
          <div className="mt-2 space-y-2 text-slate-700">
            <p>Docker Compose:</p>
            <pre className="overflow-x-auto rounded bg-slate-100 p-2 text-xs">
              {
                "docker compose logs api | grep -i 'setup code'\n# or print a new code (the old one stops working):\ndocker compose exec api baluarte-setup-code"
              }
            </pre>
            <p>Kubernetes (Helm):</p>
            <pre className="overflow-x-auto rounded bg-slate-100 p-2 text-xs">
              {
                "kubectl logs deploy/<release>-baluarte-api | grep -i 'setup code'\nkubectl exec deploy/<release>-baluarte-api -- baluarte-setup-code"
              }
            </pre>
          </div>
        </details>
        <button type="submit" disabled={sending} className={buttonClass}>
          {sending ? "Checking…" : "Continue"}
        </button>
      </form>
    </div>
  );
}

/** Loads a setup-session resource; a 401 means the setup session ended. */
function useSetupResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  onUnauthorized: () => void,
): { data: T | undefined; error: ApiError | undefined; retry: () => void } {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ attempt: number; data?: T; error?: ApiError } | null>(null);
  const loadRef = useRef(load);
  const unauthorizedRef = useRef(onUnauthorized);

  useEffect(() => {
    loadRef.current = load;
    unauthorizedRef.current = onUnauthorized;
  });

  useEffect(() => {
    const controller = new AbortController();
    loadRef.current(controller.signal).then(
      (data) => {
        setState({ attempt, data });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const apiError = asApiError(error);
        setState({ attempt, error: apiError });
        if (apiError.status === 401) unauthorizedRef.current();
      },
    );
    return () => {
      controller.abort();
    };
  }, [attempt]);

  const current = state?.attempt === attempt ? state : null;
  return {
    data: current?.data,
    error: current?.error,
    retry: () => {
      setAttempt((value) => value + 1);
    },
  };
}

function Loading({ what }: { what: string }) {
  return (
    <p role="status" className="text-slate-600">
      Loading {what}…
    </p>
  );
}

function StepNav({
  back,
  next,
  nextLabel = "Continue",
  extra,
}: {
  back?: () => void;
  next?: () => void;
  nextLabel?: string;
  extra?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap gap-2 border-t border-slate-200 pt-4">
      {back && (
        <button type="button" onClick={back} className={secondaryClass}>
          Back
        </button>
      )}
      {next && (
        <button type="button" onClick={next} className={secondaryClass}>
          {nextLabel}
        </button>
      )}
      {extra}
    </div>
  );
}

function oidcRows(config: OidcConfig): [string, ReactNode][] {
  return [
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
  ];
}

function gitlabRows(config: GitLabConfig): [string, ReactNode][] {
  return [
    ["GitLab address", config.url],
    ["Access token", secretLabel(config.token)],
    ["Request timeout", `${String(config.timeout_seconds)} seconds`],
    ["Collection", describeInterval(config.sync_interval_minutes)],
  ];
}

interface StepProps {
  status: SetupStatus;
  csrf: string;
  onSessionLost: () => void;
  onChanged: () => void;
  go: (step: Step) => void;
}

function OidcStep({ status, csrf, onSessionLost, onChanged, go }: StepProps) {
  const config = useSetupResource((signal) => getOidcConfig(SETUP, signal), onSessionLost);
  const deployment = status.oidc?.source === "deployment";

  if (config.error) return <ErrorMessage error={config.error} retry={config.retry} />;
  if (config.data === undefined) return <Loading what="the identity provider settings" />;

  if (deployment) {
    return (
      <div className="space-y-4">
        <DeploymentNotice what="Sign-in" />
        {config.data && <Values rows={oidcRows(config.data)} />}
        <StepNav
          next={() => {
            go("gitlab");
          }}
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-700">
        Baluarte signs people in with your OpenID Connect provider and gives them roles from their
        groups. Register Baluarte as an application with the provider first, then enter its details
        here.
      </p>
      <OidcForm
        base={SETUP}
        csrf={csrf}
        config={config.data}
        secretKeyConfigured={status.secret_key_configured}
        onSaved={() => {
          onChanged();
          go("gitlab");
        }}
        onUnauthorized={onSessionLost}
        submitLabel="Save and continue"
      />
      {status.oidc?.configured && (
        <StepNav
          next={() => {
            go("gitlab");
          }}
          nextLabel="Continue without changes"
        />
      )}
    </div>
  );
}

function GitLabStep({ status, csrf, onSessionLost, onChanged, go }: StepProps) {
  const config = useSetupResource((signal) => getGitLabConfig(SETUP, signal), onSessionLost);
  const deployment = status.gitlab?.source === "deployment";
  const back = () => {
    go("oidc");
  };
  const next = () => {
    go("review");
  };

  if (config.error) return <ErrorMessage error={config.error} retry={config.retry} />;
  if (config.data === undefined) return <Loading what="the GitLab settings" />;

  if (deployment) {
    return (
      <div className="space-y-4">
        <DeploymentNotice what="The GitLab connection" />
        {config.data && <Values rows={gitlabRows(config.data)} />}
        <StepNav back={back} next={next} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-700">
        Connect the self-managed GitLab instance Baluarte governs. This step is optional: you can
        connect GitLab later from Settings.
      </p>
      <GitLabForm
        base={SETUP}
        csrf={csrf}
        config={config.data}
        secretKeyConfigured={status.secret_key_configured}
        onSaved={() => {
          onChanged();
          next();
        }}
        onUnauthorized={onSessionLost}
        submitLabel="Save and continue"
      />
      <StepNav
        back={back}
        next={next}
        nextLabel={status.gitlab?.configured ? "Continue without changes" : "Skip for now"}
      />
    </div>
  );
}

function ReviewStep({ status, onSessionLost, go }: StepProps) {
  const oidc = useSetupResource((signal) => getOidcConfig(SETUP, signal), onSessionLost);
  const gitlab = useSetupResource((signal) => getGitLabConfig(SETUP, signal), onSessionLost);

  const error = oidc.error ?? gitlab.error;
  if (error) {
    return (
      <ErrorMessage
        error={error}
        retry={() => {
          oidc.retry();
          gitlab.retry();
        }}
      />
    );
  }
  if (oidc.data === undefined || gitlab.data === undefined) return <Loading what="the settings" />;

  return (
    <div className="space-y-6">
      <section aria-labelledby="review-oidc" className={cardClass}>
        <h3 id="review-oidc" className="font-semibold">
          Identity provider
        </h3>
        {oidc.data ? (
          <Values rows={oidcRows(oidc.data)} />
        ) : (
          <p role="alert" className="text-sm text-red-800">
            Not configured. Nobody can sign in until it is.
          </p>
        )}
        <button
          type="button"
          onClick={() => {
            go("oidc");
          }}
          className={secondaryClass}
        >
          Change identity provider
        </button>
      </section>
      <section aria-labelledby="review-gitlab" className={cardClass}>
        <h3 id="review-gitlab" className="font-semibold">
          GitLab
        </h3>
        {gitlab.data ? (
          <Values rows={gitlabRows(gitlab.data)} />
        ) : (
          <p className="text-sm text-slate-700">
            Not connected. An administrator can connect it from Settings after signing in.
          </p>
        )}
        <button
          type="button"
          onClick={() => {
            go("gitlab");
          }}
          className={secondaryClass}
        >
          Change GitLab connection
        </button>
      </section>
      {oidc.data && status.oidc?.configured !== false && (
        <section aria-labelledby="review-finish" className={cardClass}>
          <h3 id="review-finish" className="font-semibold">
            Finish
          </h3>
          <p className="text-sm text-slate-700">
            Sign in with an account in one of the admin groups. That first admin sign-in completes
            setup: the setup code stops working and further changes are made from Settings. If
            sign-in fails, come back here with the setup code and check the identity provider.
          </p>
          <SignInToFinish />
        </section>
      )}
    </div>
  );
}

function Wizard({ status, csrf, onSessionLost, onChanged }: Omit<StepProps, "go">) {
  const [step, setStep] = useState<Step>("oidc");
  const heading = useRef<HTMLHeadingElement>(null);
  const moved = useRef(false);

  useEffect(() => {
    // Move focus to the new step's heading, but not on first render.
    if (moved.current) heading.current?.focus();
  }, [step]);

  const go = (next: Step) => {
    moved.current = true;
    setStep(next);
  };
  const current = steps.findIndex((item) => item.step === step);
  const props = { status, csrf, onSessionLost, onChanged, go };

  return (
    <div className="space-y-6">
      <nav aria-label="Setup steps">
        <ol className="flex flex-wrap gap-2 text-sm">
          {steps.map((item, index) => (
            <li
              key={item.step}
              aria-current={item.step === step ? "step" : undefined}
              className={`rounded px-3 py-1.5 ${
                item.step === step
                  ? "bg-slate-900 text-white"
                  : index < current
                    ? "bg-slate-200 text-slate-800"
                    : "border border-slate-200 text-slate-600"
              }`}
            >
              {`${String(index + 1)}. ${item.label}`}
            </li>
          ))}
        </ol>
      </nav>
      {status.secret_key_configured === false && <SecretKeyNotice />}
      <section aria-labelledby="step-heading" className={cardClass}>
        <h2 id="step-heading" ref={heading} tabIndex={-1} className="text-lg font-semibold">
          {`Step ${String(current + 1)} of ${String(steps.length)}: ${steps[current]?.label ?? ""}`}
        </h2>
        {step === "oidc" && <OidcStep {...props} />}
        {step === "gitlab" && <GitLabStep {...props} />}
        {step === "review" && <ReviewStep {...props} />}
      </section>
    </div>
  );
}

function Complete() {
  const { session } = useAuth();
  const admin =
    session.status === "signed_in" && session.user.permissions.includes("settings:manage");
  return (
    <div className={cardClass}>
      <p>Setup is complete.</p>
      {session.status === "signed_in" ? (
        <p className="text-sm text-slate-700">
          {admin ? (
            <>
              Change the identity provider or GitLab connection on the{" "}
              <Link to="/settings" className={linkClass}>
                Settings
              </Link>{" "}
              page.
            </>
          ) : (
            <Link to="/" className={linkClass}>
              Go to the dashboard
            </Link>
          )}
        </p>
      ) : (
        <a href={signInHref("/")} className={primaryLinkClass}>
          Sign in
        </a>
      )}
    </div>
  );
}

/** The first-run installation wizard (BAL-035). Outside the signed-in shell by design. */
export function SetupPage() {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{
    attempt: number;
    status?: SetupStatus;
    error?: ApiError;
  } | null>(null);
  const [csrf, setCsrf] = useState<string | null>(readCsrf);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getSetupStatus(controller.signal).then(
      (status) => {
        setResult({ attempt, status });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setResult({ attempt, error: asApiError(error) });
      },
    );
    return () => {
      controller.abort();
    };
  }, [attempt]);

  const reload = () => {
    setAttempt((value) => value + 1);
  };
  const sessionLost = () => {
    storeCsrf(null);
    setCsrf(null);
    setNotice("Your setup session ended. Enter the setup code again to continue.");
    reload();
  };
  // Keep the last status on screen while it is re-checked, so the wizard does not lose its step.
  const status = result?.status;
  const error = result?.attempt === attempt ? result.error : undefined;

  let body: ReactNode;
  if (error) {
    body = <ErrorMessage error={error} retry={reload} />;
  } else if (!status) {
    body = (
      <p role="status" className="text-slate-600">
        Checking setup status…
      </p>
    );
  } else if (status.state === "complete") {
    body = <Complete />;
  } else if (!status.setup_session || !csrf) {
    body = (
      <CodeStep
        awaiting={status.state === "awaiting_admin_sign_in"}
        notice={notice}
        onAccepted={(token) => {
          storeCsrf(token);
          setCsrf(token);
          setNotice(null);
          reload();
        }}
      />
    );
  } else {
    body = <Wizard status={status} csrf={csrf} onSessionLost={sessionLost} onChanged={reload} />;
  }

  return (
    <section aria-labelledby="page-title" className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 id="page-title" className="text-2xl font-semibold">
          Set up Baluarte
        </h1>
        <p className="mt-2 text-slate-600">
          Connect your identity provider for sign-in and, optionally, the GitLab instance to govern.
          Secrets are stored encrypted and are never shown again.
        </p>
      </div>
      {body}
    </section>
  );
}
