import { createContext, useContext } from "react";

export interface AsOfState {
  /** The instant this page's data is shown as of, or null for current data. */
  asOf: string | null;
  /** The chosen instant, kept while visiting pages that only show current data. */
  chosen: string | null;
  /** Choose an instant, or null to go back to now. */
  choose: (asOf: string | null) => void;
}

const current: AsOfState = {
  asOf: null,
  chosen: null,
  choose: () => undefined,
};

export const AsOfContext = createContext<AsOfState>(current);

export function useAsOfState(): AsOfState {
  return useContext(AsOfContext);
}

/** The instant this page's requests are made as of, or null for current data. */
export function useAsOf(): string | null {
  return useContext(AsOfContext).asOf;
}
