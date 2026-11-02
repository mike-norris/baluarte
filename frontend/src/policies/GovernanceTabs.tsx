import type { ReactNode } from "react";
import { Link, NavLink } from "react-router";

import { listPolicyChanges } from "../api/policies";
import { useLoad } from "../api/useLoad";
import { linkClass } from "../components/styles";
import { APPLY_VIEW, CHANGES_VIEW, GOVERNANCE, SECURITY_POLICIES_VIEW } from "./format";

const tabs = [
  { to: GOVERNANCE, label: "Baselines" },
  { to: APPLY_VIEW, label: "Where baselines apply" },
  { to: SECURITY_POLICIES_VIEW, label: "GitLab security policies" },
  { to: CHANGES_VIEW, label: "Changes" },
] as const;

/**
 * Whether baseline changes need a second person, and how many wait for one. Shown on every
 * Governance view; when it cannot be read it shows nothing rather than guessing.
 */
export function ReviewStatus() {
  const pending = useLoad("policy-changes:proposed", (signal) =>
    listPolicyChanges({ state: "proposed" }, signal),
  );
  const page = pending.data;
  if (!page) return null;
  const waiting = page.items.length;
  const count = page.next_before_id === null ? String(waiting) : `${String(waiting)}+`;
  if (!page.review_required) {
    return (
      <p
        role="status"
        className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950"
      >
        <span className="font-medium">Baseline changes take effect without review.</span>
        {" New versions and where baselines apply change as soon as one person saves them. An "}
        administrator can require a second person to approve them in Settings.
        {waiting > 0 && (
          <>
            {` ${count} earlier ${waiting === 1 ? "proposal is" : "proposals are"} still waiting: `}
            <Link to={CHANGES_VIEW} className={linkClass}>
              review them
            </Link>
            .
          </>
        )}
      </p>
    );
  }
  if (waiting === 0) return null;
  return (
    <p role="status" className="rounded border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
      {`${count} baseline ${waiting === 1 ? "change is" : "changes are"} waiting for review. `}
      <Link to={CHANGES_VIEW} className={linkClass}>
        Review changes
      </Link>
    </p>
  );
}

/** The Governance heading, its tabs and an optional action, shared by the three views. */
export function GovernanceHeader({ intro, action }: { intro: ReactNode; action?: ReactNode }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 id="page-title" className="text-2xl font-semibold">
            Governance
          </h1>
          <p className="mt-2 max-w-3xl text-slate-600">{intro}</p>
        </div>
        {action}
      </div>
      <ReviewStatus />
      <nav aria-label="Governance views">
        <ul className="flex flex-wrap gap-1 border-b border-slate-200">
          {tabs.map((tab) => (
            <li key={tab.to}>
              <NavLink
                to={tab.to}
                end={tab.to === GOVERNANCE || tab.to === APPLY_VIEW}
                className={({ isActive }) =>
                  `-mb-px block border-b-2 px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${
                    isActive
                      ? "border-slate-900 text-slate-900"
                      : "border-transparent text-slate-600 hover:text-slate-900"
                  }`
                }
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
