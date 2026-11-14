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
      className={limited ? "alert alert-warning text-body" : "text-secondary mb-3"}
    >
      {shown.statement}
    </p>
  );
}
