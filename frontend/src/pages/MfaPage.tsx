import { useEffect, useId, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  enrollTotp,
  getMfaStatus,
  verifyTotp,
  type MfaEnrollment,
  type MfaStatus,
  type MfaVerified,
} from "../api/mfa";
import { QrCode } from "../auth/QrCode";
import { signInHref } from "../auth/session";

type Loaded = { status: MfaStatus } | { error: ApiError } | null;

/** Leave for the page the sign-in started from, reloading so the session is read afresh. */
function go(path: string) {
  window.location.assign(path);
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section aria-labelledby="page-title" className="container container-tight py-4">
      <div className="card card-md">
        <div className="card-body stack-3">
          <h1 id="page-title" className="h2 text-center mb-0">
            Confirm it&apos;s you
          </h1>
          {children}
        </div>
      </div>
    </section>
  );
}

function SignInAgain({ message, returnTo }: { message: string; returnTo: string }) {
  return (
    <>
      <p role="alert" className="alert alert-danger text-body">
        {message}
      </p>
      <a href={signInHref(returnTo)} className="btn btn-primary w-100">
        Sign in again
      </a>
    </>
  );
}

function RecoveryCodes({ verified }: { verified: MfaVerified }) {
  return (
    <>
      <p className="alert alert-success text-body mb-0">Your authenticator app is set up.</p>
      <p>
        Save these recovery codes somewhere safe, such as a password manager. Each one signs you in
        once if you lose your phone. They are not shown again.
      </p>
      <ul className="list-unstyled font-monospace fs-3 row row-cols-2 g-1 text-center">
        {verified.recovery_codes.map((code) => (
          <li key={code} className="col">
            {code}
          </li>
        ))}
      </ul>
      <button
        type="button"
        className="btn btn-primary w-100"
        onClick={() => go(verified.return_to)}
      >
        I have saved these codes
      </button>
    </>
  );
}

/** The second step of sign-in when Baluarte asks for a code from an authenticator app. */
export function MfaPage() {
  const id = useId();
  const [params] = useSearchParams();
  const returnTo = params.get("return_to") ?? "/";
  const [loaded, setLoaded] = useState<Loaded>(null);
  const [enrollment, setEnrollment] = useState<MfaEnrollment | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [ended, setEnded] = useState<string | null>(null);
  const [verified, setVerified] = useState<MfaVerified | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getMfaStatus(controller.signal).then(
      (status) => setLoaded({ status }),
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({
          error:
            error instanceof ApiError ? error : new ApiError(0, "The API could not be reached."),
        });
      },
    );
    return () => controller.abort();
  }, []);

  if (loaded === null) {
    return (
      <Shell>
        <p className="text-secondary text-center">Loading…</p>
      </Shell>
    );
  }
  if ("error" in loaded) {
    const message =
      loaded.error.status === 401
        ? "This sign-in has expired or was completed. Sign in again."
        : loaded.error.message;
    return (
      <Shell>
        <SignInAgain message={message} returnTo={returnTo} />
      </Shell>
    );
  }
  const { status } = loaded;
  if (ended) {
    return (
      <Shell>
        <SignInAgain message={ended} returnTo={returnTo} />
      </Shell>
    );
  }
  if (verified && verified.recovery_codes.length > 0) {
    return (
      <Shell>
        <RecoveryCodes verified={verified} />
      </Shell>
    );
  }

  async function startEnrollment() {
    setBusy(true);
    setFailure(null);
    try {
      setEnrollment(await enrollTotp(status.csrf_token));
    } catch (error) {
      setFailure(error instanceof ApiError ? error.message : "Setup could not start.");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!code.trim()) {
      setFailure("Enter the code from your authenticator app.");
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      const result = await verifyTotp(code.trim(), returnTo, status.csrf_token);
      if (result.recovery_codes.length > 0) {
        setVerified(result);
      } else {
        go(result.return_to);
      }
    } catch (error) {
      const apiError = error instanceof ApiError ? error : new ApiError(0, "Not checked.");
      if (apiError.status === 401) {
        setEnded("Too many wrong codes. Sign in again to try again.");
      } else {
        setFailure(apiError.message);
        setCode("");
      }
    } finally {
      setBusy(false);
    }
  }

  const needsSetup = !status.enrolled;
  const codeForm = (
    <form onSubmit={(event) => void submit(event)} noValidate className="stack-2">
      <label className="d-block fw-medium" htmlFor={`${id}-code`}>
        {needsSetup ? "Code from the app" : "Authentication code"}
      </label>
      <input
        id={`${id}-code`}
        name="code"
        value={code}
        onChange={(event) => setCode(event.currentTarget.value)}
        autoComplete="one-time-code"
        inputMode={needsSetup ? "numeric" : "text"}
        aria-describedby={`${id}-code-help`}
        className="form-control form-control-lg text-center font-monospace"
      />
      <p id={`${id}-code-help`} className="small text-secondary">
        {needsSetup
          ? "Enter the six-digit code the app shows for Baluarte."
          : "Enter the six-digit code from your authenticator app, or one of your recovery codes."}
      </p>
      {failure && (
        <p role="alert" className="alert alert-danger text-body">
          {failure}
        </p>
      )}
      <button type="submit" disabled={busy} className="btn btn-primary w-100">
        {busy ? "Checking…" : "Continue"}
      </button>
    </form>
  );

  if (!needsSetup) {
    return (
      <Shell>
        <p className="text-secondary text-center">Signed in as {status.account}.</p>
        {codeForm}
        <p className="small text-secondary">
          Lost your phone and your recovery codes? Ask an administrator to reset your authenticator.
        </p>
      </Shell>
    );
  }

  return (
    <Shell>
      <p>
        Baluarte asks for a second factor at sign-in. Set up an authenticator app, such as Authy,
        Google Authenticator, 1Password or Microsoft Authenticator, for {status.account}.
      </p>
      {enrollment === null ? (
        <>
          {failure && (
            <p role="alert" className="alert alert-danger text-body">
              {failure}
            </p>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => void startEnrollment()}
            className="btn btn-primary w-100"
          >
            {busy ? "Starting…" : "Set up an authenticator app"}
          </button>
        </>
      ) : (
        <>
          <ol className="ps-3 stack-1">
            <li>In the app, add an account and scan this code.</li>
            <li>Enter the code the app then shows.</li>
          </ol>
          <QrCode rows={enrollment.qr} label="QR code to add Baluarte to your authenticator app" />
          <details>
            <summary>Can&apos;t scan it?</summary>
            <p className="mt-2 small">
              Enter this key in the app, as a time-based code:{" "}
              <code className="user-select-all text-break">{enrollment.secret}</code>
            </p>
          </details>
          {codeForm}
        </>
      )}
    </Shell>
  );
}
