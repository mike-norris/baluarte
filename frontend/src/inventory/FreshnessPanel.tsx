import type { CollectionKind, FreshnessReport } from "../api/gitlab";
import { formatTime, freshnessLabels, freshnessTone } from "./format";

const kinds: { kind: CollectionKind; label: string }[] = [
  { kind: "projects", label: "Projects" },
  { kind: "groups", label: "Groups" },
  { kind: "capabilities", label: "Capabilities" },
];

function headline(report: FreshnessReport): string {
  const hours = Math.round(report.stale_after_minutes / 60);
  switch (report.status) {
    case "failed":
      return "The latest collection failed. The inventory below may be out of date.";
    case "never":
      return "No collection has completed yet, so the inventory may be empty or incomplete.";
    case "stale":
      return `Inventory data is stale: the last successful collection is more than ${String(hours)} hours old.`;
    case "partial":
      return "The latest collection finished, but some resources could not be confirmed.";
    case "fresh":
      return "Inventory is up to date.";
    case "not_configured":
      return "GitLab is not configured.";
  }
}

export function FreshnessPanel({ report }: { report: FreshnessReport }) {
  if (report.status === "not_configured") {
    return (
      <section aria-labelledby="freshness-title" className="rounded border border-slate-300 p-4">
        <h2 id="freshness-title" className="font-semibold">
          GitLab is not configured
        </h2>
        <p className="mt-1 text-sm text-slate-700">
          An administrator needs to set the GitLab URL and a read-only token before the inventory
          can be collected.
        </p>
      </section>
    );
  }
  const urgent = report.status === "failed" || report.status === "stale";
  return (
    <section
      aria-labelledby="freshness-title"
      className={`rounded border p-4 ${freshnessTone[report.status]}`}
    >
      <h2 id="freshness-title" className="font-semibold">
        Collection status: {freshnessLabels[report.status]}
      </h2>
      <p className="mt-1 text-sm" role={urgent ? "alert" : undefined}>
        {headline(report)}
      </p>
      <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-3">
        {kinds.map(({ kind, label }) => {
          const entry = report.collections[kind];
          if (!entry) return null;
          const lastRun = entry.last_run;
          return (
            <div key={kind} className="rounded border border-current/20 bg-white/60 p-3">
              <dt className="font-medium">{label}</dt>
              <dd>
                {freshnessLabels[entry.status]}
                {entry.running && " (collecting now)"}
              </dd>
              <dd className="text-slate-700">Last success: {formatTime(entry.last_success_at)}</dd>
              {lastRun?.error && (lastRun.status === "failed" || lastRun.status === "partial") && (
                <dd className="mt-1 break-words text-slate-800">
                  Last run {lastRun.status === "failed" ? "failed" : "was partial"}:{" "}
                  {lastRun.error}
                </dd>
              )}
            </div>
          );
        })}
      </dl>
    </section>
  );
}
