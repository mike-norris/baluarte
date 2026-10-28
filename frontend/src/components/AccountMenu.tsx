import { useState } from "react";

import { useAuth } from "../auth/context";
import { displayName } from "../auth/session";

export function AccountMenu() {
  const { session, signOut } = useAuth();
  const [failed, setFailed] = useState(false);

  if (session.status !== "signed_in") return null;
  const { user } = session;

  const handleSignOut = () => {
    setFailed(false);
    signOut().catch(() => {
      setFailed(true);
    });
  };

  return (
    <div className="flex items-center gap-3 text-sm md:ml-auto">
      <p>
        <span className="font-medium">{displayName(user)}</span>
        <span className="text-slate-500"> ({user.roles.join(", ")})</span>
      </p>
      <button
        type="button"
        onClick={handleSignOut}
        className="rounded border border-slate-300 px-3 py-1.5 font-medium hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
      >
        Sign out
      </button>
      {failed && (
        <p role="alert" className="text-red-700">
          Sign-out failed. Try again.
        </p>
      )}
    </div>
  );
}
