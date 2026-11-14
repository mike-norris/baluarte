import { useSyncExternalStore } from "react";

export type DeviceClass = "phone" | "tablet" | "desktop";

/**
 * Phones are narrow screens, plus touch screens too short to be a tablet, so turning a phone
 * sideways does not make it a tablet. Breakpoints are Bootstrap's md (768px) and xl (1200px).
 */
const PHONE = "(max-width: 767.98px), (pointer: coarse) and (max-height: 500px)";
const TABLET = "(min-width: 768px) and (max-width: 1199.98px)";

function matches(query: string): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(query).matches;
}

export function deviceClass(): DeviceClass {
  if (matches(PHONE)) return "phone";
  if (matches(TABLET)) return "tablet";
  return "desktop";
}

function subscribe(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const lists = [PHONE, TABLET].map((query) => window.matchMedia(query));
  for (const list of lists) list.addEventListener("change", onChange);
  return () => {
    for (const list of lists) list.removeEventListener("change", onChange);
  };
}

/** The device class for the current window; desktop where media queries are unavailable. */
export function useDeviceClass(): DeviceClass {
  return useSyncExternalStore(subscribe, deviceClass, () => "desktop");
}
