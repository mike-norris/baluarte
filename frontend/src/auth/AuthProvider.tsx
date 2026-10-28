import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { AuthContext } from "./context";
import { fetchSession, signOut, type Session } from "./session";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>({ status: "loading" });

  // Initial load: state is already "loading", so only the result is set.
  useEffect(() => {
    const controller = new AbortController();
    fetchSession(controller.signal).then(setSession, () => {
      // Aborted on unmount; nothing to update.
    });
    return () => {
      controller.abort();
    };
  }, []);

  const refresh = useCallback(async () => {
    setSession({ status: "loading" });
    setSession(await fetchSession());
  }, []);

  const endSession = useCallback(async () => {
    if (session.status !== "signed_in") return;
    await signOut(session.user.csrf_token);
    setSession({ status: "signed_out" });
  }, [session]);

  const value = useMemo(
    () => ({ session, refresh, signOut: endSession }),
    [session, refresh, endSession],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}
