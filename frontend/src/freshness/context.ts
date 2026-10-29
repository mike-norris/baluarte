import { createContext, useContext } from "react";

import type { ApiError } from "../api/client";
import type { FreshnessReport } from "../api/gitlab";
import { worst, type SignalStatus } from "./status";

/**
 * How current one source of data is. Collection is the only source today; policy evaluation
 * (BAL-021) will add an ``evaluation`` signal from its own endpoint, and the overall status
 * becomes the worst of both without changing the header or the pages that read it.
 */
export interface FreshnessSignal {
  source: "collection" | "evaluation";
  label: string;
  status: SignalStatus;
  /** The oldest last success across the source's parts; null if any part never succeeded. */
  lastSuccessAt: string | null;
}

export interface FreshnessState {
  overall: SignalStatus;
  signals: FreshnessSignal[];
  report: FreshnessReport | undefined;
  error: ApiError | undefined;
}

/** Null until the user is signed in. */
export const FreshnessContext = createContext<FreshnessState | null>(null);

export function useFreshness(): FreshnessState | null {
  return useContext(FreshnessContext);
}

function oldestSuccess(report: FreshnessReport): string | null {
  const times = Object.values(report.collections).map((entry) => entry.last_success_at);
  if (times.length === 0 || times.includes(null)) return null;
  return times.reduce((oldest, time) =>
    time !== null && oldest !== null && Date.parse(time) < Date.parse(oldest) ? time : oldest,
  );
}

export function collectionSignal(
  report: FreshnessReport | undefined,
  error: ApiError | undefined,
): FreshnessSignal {
  const status: SignalStatus = error ? "unknown" : report ? report.status : "checking";
  return {
    source: "collection",
    label: "GitLab collection",
    status,
    lastSuccessAt: report && !error ? oldestSuccess(report) : null,
  };
}

export function freshnessState(
  report: FreshnessReport | undefined,
  error: ApiError | undefined,
): FreshnessState {
  const signals = [collectionSignal(report, error)];
  return {
    overall: worst(signals.map((signal) => signal.status)),
    signals,
    report,
    error,
  };
}
