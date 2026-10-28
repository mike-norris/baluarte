import { Outlet, useLocation } from "react-router";

import { useAuth } from "./context";
import { signInHref } from "./session";

const buttonClass =
  "inline-block rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600";

/** Renders child routes only with a session; otherwise explains why and offers sign-in. */
export function RequireSession() {
  const { session, refresh } = useAuth();
  const location = useLocation();

  switch (session.status) {
    case "signed_in":
      return <Outlet />;
    case "loading":
      return (
        <p role="status" className="text-slate-600">
          Checking your session…
        </p>
      );
    case "signed_out":
      return (
        <section aria-labelledby="page-title">
          <h1 id="page-title" className="text-2xl font-semibold">
            Sign in to Baluarte
          </h1>
          <p className="mt-2 text-slate-600">
            Use your organization&apos;s single sign-on account to continue.
          </p>
          <a href={signInHref(location.pathname + location.search)} className={`mt-6 ${buttonClass}`}>
            Sign in
          </a>
        </section>
      );
    case "not_configured":
      return (
        <section aria-labelledby="page-title">
          <h1 id="page-title" className="text-2xl font-semibold">
            Sign-in is not configured
          </h1>
          <p className="mt-2 text-slate-600">
            An administrator needs to configure the OIDC identity provider before anyone can sign
            in.
          </p>
        </section>
      );
    case "error":
      return (
        <section aria-labelledby="page-title">
          <h1 id="page-title" className="text-2xl font-semibold">
            Baluarte is unavailable
          </h1>
          <p className="mt-2 text-slate-600">The API could not be reached.</p>
          <button type="button" onClick={() => void refresh()} className={`mt-6 ${buttonClass}`}>
            Try again
          </button>
        </section>
      );
  }
}
