/**
 * The past-date view: ``?as_of=`` in the URL, a UTC instant. Pages that can show the state at
 * that instant (findings, controls, the dashboard, a project and changes) pass it to every
 * request; the API answers from the latest evaluation run finished at or before it.
 */

import { formatTime } from "../time";

export const AS_OF_PARAM = "as_of";

/** The evaluation run a past-date answer came from; null when none had finished by then. */
export interface AsOfRun {
  id: number;
  finished_at: string | null;
}

/** What the API adds to a response made as of a past date. */
export interface AsOfAnswer {
  as_of?: string | null;
  evaluation_run?: AsOfRun | null;
}

const INSTANT = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?Z$/;
const LOCAL = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

/** Paths whose pages show data as of a past date; every other page shows current data. */
const AWARE: readonly RegExp[] = [
  /^\/$/,
  /^\/findings(\/[^/]+)?\/?$/,
  /^\/controls(\/[^/]+\/[^/]+)?\/?$/,
  /^\/inventory\/projects\/[^/]+\/?$/,
  /^\/changes(\/[^/]+)?\/?$/,
];

export function asOfAware(pathname: string): boolean {
  return AWARE.some((pattern) => pattern.test(pathname));
}

/** A URL value as ``YYYY-MM-DDTHH:MM:00Z``, or null when it is missing or not a UTC instant. */
export function parseAsOf(value: string | null): string | null {
  const match = INSTANT.exec(value?.trim() ?? "");
  if (!match?.[1]) return null;
  const instant = `${match[1]}:00Z`;
  return Number.isNaN(Date.parse(instant)) ? null : instant;
}

/** A ``datetime-local`` value (read as UTC) as an instant, or null when invalid. */
export function fromLocal(value: string): string | null {
  return LOCAL.test(value) ? parseAsOf(`${value}:00Z`) : null;
}

/** An instant as a ``datetime-local`` value in UTC. */
export function toLocal(instant: string): string {
  return instant.slice(0, 16);
}

/** The current minute in UTC, as a ``datetime-local`` value: the latest date that can be chosen. */
export function nowLocal(now: Date = new Date()): string {
  return now.toISOString().slice(0, 16);
}

/** Why a chosen date can't be used, or null when it can. */
export function asOfProblem(value: string, now: Date = new Date()): string | null {
  const instant = fromLocal(value);
  if (instant === null) return "Enter a date and time.";
  if (Date.parse(instant) > now.getTime()) return "Choose a date and time in the past.";
  return null;
}

/** ``href`` with ``as_of`` set, or removed when ``asOf`` is null. */
export function withAsOf(href: string, asOf: string | null): string {
  const [path = "", query = ""] = href.split("?", 2);
  const params = new URLSearchParams(query);
  if (asOf === null) params.delete(AS_OF_PARAM);
  else params.set(AS_OF_PARAM, asOf);
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

export function asOfText(asOf: string): string {
  return formatTime(asOf);
}

/** Shown wherever a change would be made, while the page shows the past. */
export const pastReadOnly =
  "You are viewing the past, so nothing can be changed here. Go back to now to make changes.";
