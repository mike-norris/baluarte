import { useEffect, useEffectEvent, useMemo, useState, type ReactNode } from "react";
import { useLocation } from "react-router";

import { ApiError } from "../api/client";
import { getFreshness, type FreshnessReport } from "../api/gitlab";
import { useAuth } from "../auth/context";
import { FreshnessContext, freshnessState } from "./context";

/** How often the header re-checks while a page stays open. */
export const REFRESH_INTERVAL_MS = 60_000;

type Result = { report: FreshnessReport } | { error: ApiError };

/**
 * Checks collection freshness while ``enabled``: on mount, on every navigation and every
 * REFRESH_INTERVAL_MS. The previous result stays visible while a re-check is in flight, and a
 * failed check replaces it, so an old "up to date" is never shown once the status is unknown.
 */
export function FreshnessProvider({
  enabled,
  children,
}: {
  enabled: boolean;
  children: ReactNode;
}) {
  const { pathname } = useLocation();
  const { refresh } = useAuth();
  const [tick, setTick] = useState(0);
  const [result, setResult] = useState<Result | null>(null);
  const onUnauthorized = useEffectEvent(() => void refresh());

  useEffect(() => {
    if (!enabled) return;
    const timer = setInterval(() => {
      setTick((value) => value + 1);
    }, REFRESH_INTERVAL_MS);
    return () => {
      clearInterval(timer);
    };
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    getFreshness(controller.signal).then(
      (report) => {
        setResult({ report });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const apiError =
          error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
        setResult({ error: apiError });
        if (apiError.status === 401) onUnauthorized();
      },
    );
    return () => {
      controller.abort();
    };
  }, [enabled, pathname, tick]);

  const value = useMemo(() => {
    if (!enabled) return null;
    return freshnessState(
      result && "report" in result ? result.report : undefined,
      result && "error" in result ? result.error : undefined,
    );
  }, [enabled, result]);

  return <FreshnessContext value={value}>{children}</FreshnessContext>;
}
