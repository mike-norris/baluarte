import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate } from "react-router";

import { ApiError } from "../api/client";
import { getProfile, SETUP_PROFILE, type Profile } from "../api/compliance";
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
import { buttonClass, fieldClass } from "../components/styles";
import { ComplianceForm } from "../compliance/ComplianceForm";
import { frameworkList, settingInfo, settingOrder } from "../compliance/format";
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
import { PageHeader } from "../ui/PageHeader";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";

type Step = "oidc" | "gitlab" | "compliance" | "review";

const allSteps: { step: Step; label: string }[] = [
  { step: "oidc", label: "Identity provider" },
  { step: "gitlab", label: "GitLab" },
  { step: "compliance", label: "Compliance" },
  { step: "review", label: "Review and finish" },
];

/** Servers that predate the operating profile report no compliance section and have no compliance step. */
function stepsFor(status: SetupStatus): { step: Step; label: string }[] {
  return status.compliance === undefined
    ? allSteps.filter((item) => item.step !== "compliance")
    : allSteps;
}

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
  onCompleted,
}: {
  awaiting: boolean;
  notice: string | null;
  onAccepted: (csrf: string) => void;
  onCompleted: () => void;
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
      const apiError = asApiError(error);
      setProblems([apiError.message]);
      if (apiError.status === 409) onCompleted();
    } finally {
      setSending(false);
    }
  }

  return (
    <div>
      {notice && (
        <p role="status" className={noteClass}>
          {notice}
        </p>
      )}
      {awaiting && (
        <div className={cardClass}>
          <h2 className="card-title">Finish setup by signing in</h2>
          <p className="text-secondary">
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
        <h2 id="setup-code-heading" className="card-title">
          Enter the setup code
        </h2>
        <p className="text-secondary">
          Baluarte prints a one-time setup code in the API&apos;s log each time it starts until
          setup is complete. Only someone who can read the server&apos;s logs can install it.
        </p>
        <Problems messages={problems} />
        <label className="d-block fw-medium">
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
        <details>
          <summary className="fw-medium">Where to find the code</summary>
          <div className="mt-2 text-secondary">
            <p>Docker Compose:</p>
            <pre className="alert alert-secondary text-body">
              {
                "docker compose logs api | grep -i 'setup code'\n# or print a new code (the old one stops working):\ndocker compose exec api baluarte-setup-code"
              }
            </pre>
            <p>Kubernetes (Helm):</p>
            <pre className="alert alert-secondary text-body">
              {
                "kubectl -n <ns> logs deploy/<release>-baluarte-api | grep -i 'setup code'\nkubectl -n <ns> exec deploy/<release>-baluarte-api -- baluarte-setup-code"
              }
            </pre>
            <p className="small">
              If the release name already contains &quot;baluarte&quot;, the deployment is{" "}
              <code>&lt;release&gt;-api</code>; a <code>fullnameOverride</code> replaces the prefix
              too. <code>kubectl -n &lt;ns&gt; get deploy</code> lists the names.
            </p>
          </div>
        </details>
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Checking…" : "Continue"}
        </button>
      </form>
    </div>
  );
}

/** Loads a setup-session resource; failures also go to ``onError`` (see ``SetupPage``). */
function useSetupResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  onError: (error: ApiError) => void,
): { data: T | undefined; error: ApiError | undefined; retry: () => void } {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{
    attempt: number;
    data?: T;
    error?: ApiError;
  } | null>(null);
  const loadRef = useRef(load);
  const errorRef = useRef(onError);

  useEffect(() => {
    loadRef.current = load;
    errorRef.current = onError;
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
        errorRef.current(apiError);
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
    <p role="status" className="text-secondary">
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
    <div className="d-flex flex-wrap gap-2 border-t pt-4">
      {back && (
        <button type="button" onClick={back} className="btn">
          Back
        </button>
      )}
      {next && (
        <button type="button" onClick={next} className="btn">
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
    ["Manager groups", groupList(config.manager_groups ?? [])],
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
  onError: (error: ApiError) => void;
  onChanged: () => void;
  go: (step: Step) => void;
}

function OidcStep({ status, csrf, onError, onChanged, go }: StepProps) {
  const config = useSetupResource((signal) => getOidcConfig(SETUP, signal), onError);
  const deployment = status.oidc?.source === "deployment";

  if (config.error) return <ErrorMessage error={config.error} retry={config.retry} />;
  if (config.data === undefined) return <Loading what="the identity provider settings" />;

  if (deployment) {
    return (
      <div>
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
    <div>
      <p className="text-secondary">
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
        onError={onError}
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

function GitLabStep({ status, csrf, onError, onChanged, go }: StepProps) {
  const config = useSetupResource((signal) => getGitLabConfig(SETUP, signal), onError);
  const deployment = status.gitlab?.source === "deployment";
  const back = () => {
    go("oidc");
  };
  const next = () => {
    go(status.compliance === undefined ? "review" : "compliance");
  };

  if (config.error) return <ErrorMessage error={config.error} retry={config.retry} />;
  if (config.data === undefined) return <Loading what="the GitLab settings" />;

  if (deployment) {
    return (
      <div>
        <DeploymentNotice what="The GitLab connection" />
        {config.data && <Values rows={gitlabRows(config.data)} />}
        <StepNav back={back} next={next} />
      </div>
    );
  }

  return (
    <div>
      <p className="text-secondary">
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
        onError={onError}
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

function complianceRows(profile: Profile): [string, ReactNode][] {
  return [
    ["Frameworks", frameworkList(profile.frameworks)],
    ...settingOrder.map((name): [string, ReactNode] => {
      const value = profile.settings[name];
      const text =
        typeof value === "boolean"
          ? value
            ? "On"
            : "Off"
          : `${String(value)} ${settingInfo[name].unit ?? ""}`;
      return [settingInfo[name].label, text];
    }),
  ];
}

function ComplianceStep({ csrf, onError, onChanged, go }: StepProps) {
  const profile = useSetupResource((signal) => getProfile(SETUP_PROFILE, signal), onError);

  if (profile.error) return <ErrorMessage error={profile.error} retry={profile.retry} />;
  if (profile.data === undefined) return <Loading what="the compliance profile" />;
  const configured = profile.data.configured;

  return (
    <div>
      <p className="text-secondary">
        Should Baluarte itself meet SOC 2, ISO 27001 or NIST audit requirements? Your choice sets
        how long Baluarte keeps its audit log and evidence, how long sessions last and whether
        baseline changes need a second person. An administrator can change it later in Settings, and
        the Compliance page shows how Baluarte measures up.
      </p>
      <ComplianceForm
        path={SETUP_PROFILE}
        csrf={csrf}
        profile={profile.data}
        enforceMinimums
        submitLabel="Save and continue"
        onSaved={() => {
          onChanged();
          go("review");
        }}
        onError={onError}
      />
      <StepNav
        back={() => {
          go("gitlab");
        }}
        {...(configured
          ? {
              next: () => {
                go("review");
              },
            }
          : {})}
        nextLabel="Continue without changes"
      />
    </div>
  );
}

/** The compliance profile for review, or null on servers without one. */
function loadComplianceForReview(status: SetupStatus) {
  return (signal: AbortSignal): Promise<Profile | null> =>
    status.compliance === undefined ? Promise.resolve(null) : getProfile(SETUP_PROFILE, signal);
}

function ReviewStep({ status, onError, go }: StepProps) {
  const oidc = useSetupResource((signal) => getOidcConfig(SETUP, signal), onError);
  const gitlab = useSetupResource((signal) => getGitLabConfig(SETUP, signal), onError);
  const compliance = useSetupResource(loadComplianceForReview(status), onError);

  const error = oidc.error ?? gitlab.error ?? compliance.error;
  if (error) {
    return (
      <ErrorMessage
        error={error}
        retry={() => {
          oidc.retry();
          gitlab.retry();
          compliance.retry();
        }}
      />
    );
  }
  if (oidc.data === undefined || gitlab.data === undefined || compliance.data === undefined) {
    return <Loading what="the settings" />;
  }
  const complianceMissing = compliance.data !== null && !compliance.data.configured;

  return (
    <div>
      <section aria-labelledby="review-oidc" className={cardClass}>
        <h3 id="review-oidc" className="fw-semibold">
          Identity provider
        </h3>
        {oidc.data ? (
          <Values rows={oidcRows(oidc.data)} />
        ) : (
          <p role="alert" className="text-danger-emphasis">
            Not configured. Nobody can sign in until it is.
          </p>
        )}
        <button
          type="button"
          onClick={() => {
            go("oidc");
          }}
          className="btn"
        >
          Change identity provider
        </button>
      </section>
      <section aria-labelledby="review-gitlab" className={cardClass}>
        <h3 id="review-gitlab" className="fw-semibold">
          GitLab
        </h3>
        {gitlab.data ? (
          <Values rows={gitlabRows(gitlab.data)} />
        ) : (
          <p className="text-secondary">
            Not connected. An administrator can connect it from Settings after signing in.
          </p>
        )}
        <button
          type="button"
          onClick={() => {
            go("gitlab");
          }}
          className="btn"
        >
          Change GitLab connection
        </button>
      </section>
      {compliance.data && (
        <section aria-labelledby="review-compliance" className={cardClass}>
          <h3 id="review-compliance" className="fw-semibold">
            Compliance profile
          </h3>
          {complianceMissing ? (
            <p role="alert" className="text-danger-emphasis">
              Not chosen. Choose the frameworks Baluarte must meet, or None of these, before
              finishing.
            </p>
          ) : (
            <Values rows={complianceRows(compliance.data)} />
          )}
          <button
            type="button"
            onClick={() => {
              go("compliance");
            }}
            className="btn"
          >
            Change compliance profile
          </button>
        </section>
      )}
      {oidc.data && status.oidc?.configured !== false && !complianceMissing && (
        <section aria-labelledby="review-finish" className={cardClass}>
          <h3 id="review-finish" className="fw-semibold">
            Finish
          </h3>
          <p className="text-secondary">
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

function Wizard({ status, csrf, onError, onChanged }: Omit<StepProps, "go">) {
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
  const steps = stepsFor(status);
  const current = steps.findIndex((item) => item.step === step);
  const props = { status, csrf, onError, onChanged, go };

  return (
    <div>
      <nav aria-label="Setup steps">
        <ol className="steps steps-counter mb-4">
          {steps.map((item, index) => (
            <li
              key={item.step}
              aria-current={item.step === step ? "step" : undefined}
              className={`step-item ${item.step === step ? "active" : ""}`}
            >
              {`${String(index + 1)}. ${item.label}`}
            </li>
          ))}
        </ol>
      </nav>
      {status.secret_key_configured === false && <SecretKeyNotice />}
      <section aria-labelledby="step-heading" className={cardClass}>
        <h2 id="step-heading" ref={heading} tabIndex={-1} className="card-title">
          {`Step ${String(current + 1)} of ${String(steps.length)}: ${steps[current]?.label ?? ""}`}
        </h2>
        {step === "oidc" && <OidcStep {...props} />}
        {step === "gitlab" && <GitLabStep {...props} />}
        {step === "compliance" && <ComplianceStep {...props} />}
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
        <p className="text-secondary">
          {admin ? (
            <>
              Change the identity provider or GitLab connection on the{" "}
              <Link to="/settings">Settings</Link> page.
            </>
          ) : (
            <Link to="/">Go to the dashboard</Link>
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

/** The first-run installation wizard. Outside the signed-in shell by design. */
export function SetupPage() {
  const phone = useDeviceClass() === "phone";
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{
    attempt: number;
    status?: SetupStatus;
    error?: ApiError;
  } | null>(null);
  const [csrf, setCsrf] = useState<string | null>(readCsrf);
  const [notice, setNotice] = useState<string | null>(null);
  // Set when a setup endpoint says setup is complete: the page is left once the status agrees.
  const [leaving, setLeaving] = useState(false);
  const { session } = useAuth();

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
  const endSession = (message: string) => {
    storeCsrf(null);
    setCsrf(null);
    setNotice(message);
    reload();
  };
  const completed = () => {
    setLeaving(true);
    reload();
  };
  // Setup endpoints answer 409 once setup is complete, 401 without a setup session and 403 when
  // the CSRF token or Origin is refused. A 409 can also mean a refused change, so the status
  // decides whether to leave.
  const handleError = (failure: ApiError) => {
    if (failure.status === 409) completed();
    else if (failure.status === 401) {
      endSession("Your setup session ended. Enter the setup code again to continue.");
    } else if (failure.status === 403) {
      endSession(
        "The setup session's security check failed, for example after it was opened in another tab. Enter the setup code again to continue.",
      );
    }
  };
  // Keep the last status on screen while it is re-checked, so the wizard does not lose its step.
  const status = result?.status;
  const error = result?.attempt === attempt ? result.error : undefined;

  let body: ReactNode;
  if (phone && status?.state !== "complete") {
    body = <EmptyState title="Baluarte is set up on a larger screen." />;
  } else if (error) {
    body = <ErrorMessage error={error} retry={reload} />;
  } else if (!status) {
    body = (
      <p role="status" className="text-secondary">
        Checking setup status…
      </p>
    );
  } else if (status.state === "complete") {
    if (leaving && result?.attempt === attempt) {
      return <Navigate to={session.status === "signed_in" ? "/" : "/sign-in"} replace />;
    }
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
        onCompleted={completed}
      />
    );
  } else {
    body = <Wizard status={status} csrf={csrf} onError={handleError} onChanged={reload} />;
  }

  return (
    <section aria-labelledby="page-title" className="mx-auto" style={{ maxWidth: "48rem" }}>
      <div>
        <PageHeader
          title="Set up Baluarte"
          subtitle="Connect your identity provider for sign-in and, optionally, the GitLab instance to govern, then choose the compliance frameworks Baluarte itself must meet. Secrets are stored encrypted and are never shown again."
        />
      </div>
      {body}
    </section>
  );
}
