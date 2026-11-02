import type { Population } from "../api/gitlab";
import { useFreshness } from "./context";

/** What the inventory, and so every result, can cover: what the GitLab token's user can see. */
export function PopulationNote({ population }: { population?: Population | null | undefined }) {
  const freshness = useFreshness();
  const shown = population ?? freshness?.report?.population;
  if (!shown) return null;
  const limited = shown.basis !== "instance";
  return (
    <p
      aria-label="Inventory coverage"
      className={
        limited
          ? "mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
          : "mt-3 text-sm text-slate-700"
      }
    >
      {shown.statement}
    </p>
  );
}
