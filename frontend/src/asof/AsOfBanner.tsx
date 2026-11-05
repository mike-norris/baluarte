import { secondaryClass } from "../components/styles";
import { useAsOfState } from "./context";
import { asOfText } from "./format";

/** Shown on every page while a past date is chosen, with the way back to current data. */
export function AsOfBanner() {
  const { asOf, chosen, choose } = useAsOfState();
  if (chosen === null) return null;
  return (
    <div
      role="region"
      aria-label="Past-date view"
      className="border-b border-amber-300 bg-amber-50 text-sm text-amber-950"
    >
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-3 px-4 py-2">
        <p>
          <span className="font-semibold">{`Viewing as of ${asOfText(chosen)}.`}</span>{" "}
          {asOf
            ? "Results are shown as they stood then. Nothing can be changed in this view."
            : "This page shows current data; findings, controls, the dashboard, projects and changes show the past."}
        </p>
        <button
          type="button"
          onClick={() => {
            choose(null);
          }}
          className={secondaryClass}
        >
          Back to now
        </button>
      </div>
    </div>
  );
}
