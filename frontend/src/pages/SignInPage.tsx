import { Navigate, useSearchParams } from "react-router";

import { useAuth } from "../auth/context";
import { signInHref } from "../auth/session";

/** Messages for the error codes the API's sign-in callback redirects with. */
export const signInErrors: Record<string, string> = {
  expired: "Your sign-in attempt expired or was started in another browser. Please try again.",
  provider_denied: "The identity provider did not complete sign-in.",
  provider_unavailable: "The identity provider could not be reached. Try again later.",
  not_authorized:
    "Your account is not in a group that has access to Baluarte. Ask an administrator for access.",
  sign_in_failed: "Sign-in could not be completed. Try again, or contact an administrator.",
};

const fallbackError = signInErrors.sign_in_failed;

export function SignInPage() {
  const { session } = useAuth();
  const [params] = useSearchParams();
  const code = params.get("error");

  if (session.status === "signed_in" && !code) {
    return <Navigate to="/" replace />;
  }

  return (
    <section aria-labelledby="page-title" className="container container-tight py-4">
      <div className="card card-md">
        <div className="card-body">
          <h1 id="page-title" className="h2 text-center mb-3">
            Sign in to Baluarte
          </h1>
          {code && (
            <p role="alert" className="alert alert-danger text-body mt-3">
              {signInErrors[code] ?? fallbackError}
            </p>
          )}
          {session.status === "not_configured" ? (
            <p className="mt-3 text-secondary">Sign-in is not configured.</p>
          ) : (
            <a href={signInHref("/")} className="btn btn-primary w-100 mt-3">
              Sign in
            </a>
          )}
        </div>
      </div>
    </section>
  );
}
