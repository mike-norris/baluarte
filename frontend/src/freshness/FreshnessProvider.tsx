import { useEffect, useEffectEvent, useMemo, useState, type ReactNode } from "react";
import { useLocation } from "react-router";

import { ApiError } from "../api/client";
import { getEvaluationFreshness, type EvaluationFreshness } from "../api/evaluations";
import { getFreshness, type FreshnessReport } from "../api/gitlab";
import { useAuth } from "../auth/context";
import { FreshnessContext, freshnessState, type Checked } from "./context";

/** How often the header re-checks while a page stays open. */
export const REFRESH_INTERVAL_MS = 60_000;

function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
}

/**
 * Checks collection and evaluation freshness while ``enabled``: on mount, on every navigation
 * and every REFRESH_INTERVAL_MS. The previous results stay visible while a re-check is in
 * flight, and a failed check replaces its result, so an old "up to date" is never shown once
 * the status is unknown.
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
  const [collection, setCollection] = useState<Checked<FreshnessReport>>({});
  const [evaluation, setEvaluation] = useState<Checked<EvaluationFreshness>>({});
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
    const failed = (error: unknown): ApiError | null => {
      if (controller.signal.aborted) return null;
      const apiError = asApiError(error);
      if (apiError.status === 401) onUnauthorized();
      return apiError;
    };
    getFreshness(controller.signal).then(
      (data) => {
        setCollection({ data });
      },
      (error: unknown) => {
        const apiError = failed(error);
        if (apiError) setCollection({ error: apiError });
      },
    );
    getEvaluationFreshness(controller.signal).then(
      (data) => {
        setEvaluation({ data });
      },
      (error: unknown) => {
        const apiError = failed(error);
        if (apiError) setEvaluation({ error: apiError });
      },
    );
    return () => {
      controller.abort();
    };
  }, [enabled, pathname, tick]);

  const value = useMemo(() => {
    if (!enabled) return null;
    return freshnessState(collection, evaluation);
  }, [enabled, collection, evaluation]);

  return <FreshnessContext value={value}>{children}</FreshnessContext>;
}
