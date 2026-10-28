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
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        Sign in to Baluarte
      </h1>
      {code && (
        <p role="alert" className="mt-4 rounded border border-red-200 bg-red-50 p-4 text-red-800">
          {signInErrors[code] ?? fallbackError}
        </p>
      )}
      {session.status === "not_configured" ? (
        <p className="mt-4 text-slate-600">Sign-in is not configured.</p>
      ) : (
        <a
          href={signInHref("/")}
          className="mt-6 inline-block rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
        >
          Sign in
        </a>
      )}
    </section>
  );
}
