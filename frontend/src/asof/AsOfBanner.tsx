import { IconHistory } from "@tabler/icons-react";

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
      className="app-asof-banner alert alert-warning rounded-0 border-start-0 border-end-0 mb-0 py-2"
    >
      <div className="container-xl d-flex flex-wrap align-items-center gap-3">
        <IconHistory size={18} className="alert-icon" aria-hidden="true" />
        <p className="mb-0 flex-grow-1">
          <strong>{`Viewing as of ${asOfText(chosen)}.`}</strong>{" "}
          {asOf
            ? "Results are shown as they stood then. Nothing can be changed in this view."
            : "This page shows current data; findings, controls, the dashboard, projects and changes show the past."}
        </p>
        <button
          type="button"
          onClick={() => {
            choose(null);
          }}
          className="btn btn-sm"
        >
          Back to now
        </button>
      </div>
    </div>
  );
}
