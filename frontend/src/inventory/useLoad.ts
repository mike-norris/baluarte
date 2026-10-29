import { useCallback, useEffect, useEffectEvent, useState } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/context";

type Result<T> = { key: string; data: T } | { key: string; error: ApiError };

export interface Loaded<T> {
  loading: boolean;
  data: T | undefined;
  error: ApiError | undefined;
  retry: () => void;
}

/**
 * Loads data for ``key``, refetching when the key changes and aborting stale requests.
 * A result belongs to the key it was fetched for, so a key change shows as loading at once.
 * A 401 refreshes the session, which sends the user to sign in.
 */
export function useLoad<T>(key: string, load: (signal: AbortSignal) => Promise<T>): Loaded<T> {
  const { refresh } = useAuth();
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result<T> | null>(null);
  const requestKey = `${key}#${String(attempt)}`;
  const fetchData = useEffectEvent(load);
  const onUnauthorized = useEffectEvent(() => void refresh());

  useEffect(() => {
    const controller = new AbortController();
    fetchData(controller.signal).then(
      (data) => {
        setResult({ key: requestKey, data });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const apiError =
          error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
        setResult({ key: requestKey, error: apiError });
        if (apiError.status === 401) onUnauthorized();
      },
    );
    return () => {
      controller.abort();
    };
  }, [requestKey]);

  const retry = useCallback(() => {
    setAttempt((value) => value + 1);
  }, []);
  const current = result?.key === requestKey ? result : null;
  return {
    loading: current === null,
    data: current && "data" in current ? current.data : undefined,
    error: current && "error" in current ? current.error : undefined,
    retry,
  };
}
