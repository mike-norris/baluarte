import type { ReactNode } from "react";
import { Link, NavLink } from "react-router";

import { listPolicyChanges } from "../api/policies";
import { useLoad } from "../api/useLoad";
import { APPLY_VIEW, CHANGES_VIEW, GOVERNANCE, SECURITY_POLICIES_VIEW } from "./format";
import { PageHeader } from "../ui/PageHeader";

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
      <p role="status" className="alert alert-warning text-body">
        <span className="fw-medium">Baseline changes take effect without review.</span>
        {" New versions and where baselines apply change as soon as one person saves them. An "}
        administrator can require a second person to approve them in Settings.
        {waiting > 0 && (
          <>
            {` ${count} earlier ${waiting === 1 ? "proposal is" : "proposals are"} still waiting: `}
            <Link to={CHANGES_VIEW}>review them</Link>.
          </>
        )}
      </p>
    );
  }
  if (waiting === 0) return null;
  return (
    <p role="status" className="alert alert-info text-body">
      {`${count} baseline ${waiting === 1 ? "change is" : "changes are"} waiting for review. `}
      <Link to={CHANGES_VIEW}>Review changes</Link>
    </p>
  );
}

/** The Governance heading, its tabs and an optional action, shared by the three views. */
export function GovernanceHeader({ intro, action }: { intro: ReactNode; action?: ReactNode }) {
  return (
    <>
      <PageHeader pretitle="Govern" title="Governance" subtitle={intro} actions={action} />
      <ReviewStatus />
      <nav aria-label="Governance views" className="mb-3">
        <ul className="nav nav-tabs flex-nowrap overflow-auto">
          {tabs.map((tab) => (
            <li key={tab.to} className="nav-item">
              <NavLink
                to={tab.to}
                end={tab.to === GOVERNANCE || tab.to === APPLY_VIEW}
                className="nav-link text-nowrap"
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </>
  );
}
