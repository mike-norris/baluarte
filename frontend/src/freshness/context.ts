import { createContext, useContext } from "react";

import type { ApiError } from "../api/client";
import type { EvaluationFreshness } from "../api/evaluations";
import type { FreshnessReport } from "../api/gitlab";
import { worst, type SignalStatus } from "./status";

/**
 * How current one source of data is: GitLab collection, and policy evaluation of what was
 * collected. The overall status is the worst of both.
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
  /** The signal that sets ``overall``; collection first when both are equally bad. */
  worstSignal: FreshnessSignal;
  signals: FreshnessSignal[];
  report: FreshnessReport | undefined;
  error: ApiError | undefined;
  evaluation: EvaluationFreshness | undefined;
  evaluationError: ApiError | undefined;
}

export interface Checked<T> {
  data?: T | undefined;
  error?: ApiError | undefined;
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

export function evaluationSignal(
  report: EvaluationFreshness | undefined,
  error: ApiError | undefined,
): FreshnessSignal {
  const status: SignalStatus = error ? "unknown" : report ? report.status : "checking";
  return {
    source: "evaluation",
    label: "Policy evaluation",
    status,
    lastSuccessAt: report && !error ? report.last_success_at : null,
  };
}

export function freshnessState(
  collection: Checked<FreshnessReport>,
  evaluation: Checked<EvaluationFreshness> = {},
): FreshnessState {
  const collected = collectionSignal(collection.data, collection.error);
  const evaluated = evaluationSignal(evaluation.data, evaluation.error);
  const signals = [collected, evaluated];
  const overall = worst(signals.map((signal) => signal.status));
  return {
    overall,
    worstSignal: collected.status === overall ? collected : evaluated,
    signals,
    report: collection.data,
    error: collection.error,
    evaluation: evaluation.data,
    evaluationError: evaluation.error,
  };
}
