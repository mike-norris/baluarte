import { useState } from "react";
import { NavLink } from "react-router";

import { ApiError } from "../api/client";
import { getSettings } from "../api/setup";
import { discoverSecurityPolicies, type DiscoveryContext } from "../api/securityPolicies";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { secondaryClass } from "../components/styles";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { SECURITY_POLICIES_VIEW } from "../policies/format";
import { collectedText, COVERAGE_VIEW, discoveryProblem, typeGaps } from "./format";

export const SETTINGS_MANAGE = "settings:manage";

export const INTRO =
  "These are enforced by GitLab. Baluarte reads them to show where they apply and to check coverage. Change them in GitLab.";

function canManage(permissions: string[] | undefined): boolean {
  return permissions?.includes(SETTINGS_MANAGE) ?? false;
}

/** The Governance header with the Policies and Coverage views of GitLab security policies. */
export function SecurityPoliciesHeader({ context }: { context: DiscoveryContext | undefined }) {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  return (
    <div className="space-y-4">
      <GovernanceHeader
        intro={INTRO}
        action={
          user && canManage(user.permissions) ? <CollectNow csrf={user.csrf_token} /> : undefined
        }
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <nav aria-label="GitLab security policy views">
          <ul className="flex gap-1">
            {[
              { to: SECURITY_POLICIES_VIEW, label: "Policies" },
              { to: COVERAGE_VIEW, label: "Coverage" },
            ].map((tab) => (
              <li key={tab.to}>
                <NavLink
                  to={tab.to}
                  end
                  className={({ isActive }) =>
                    `block rounded px-3 py-1.5 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${
                      isActive ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"
                    }`
                  }
                >
                  {tab.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        {context && <p className="text-sm text-slate-600">{collectedText(context)}</p>}
      </div>
      {context && <DiscoveryNotice context={context} />}
    </div>
  );
}

/** Says when results are incomplete or unreadable, so unknown never reads as "none". */
export function DiscoveryNotice({ context }: { context: DiscoveryContext }) {
  const problem = discoveryProblem(context);
  const gaps = typeGaps(context);
  if (!problem && !gaps && context.collection.status !== "stale") return null;
  return (
    <div className="space-y-1 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
      {problem && <p>{problem}</p>}
      {!problem && context.collection.status === "stale" && (
        <p>These results are stale: the last successful collection is older than expected.</p>
      )}
      {gaps && <p>{`Not every policy type can be read: ${gaps}. Those types count as unknown.`}</p>}
    </div>
  );
}

function CollectNow({ csrf }: { csrf: string }) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "pending" }
    | { kind: "queued" }
    | { kind: "failed"; message: string }
  >({ kind: "idle" });

  async function collect() {
    setState({ kind: "pending" });
    try {
      await discoverSecurityPolicies(csrf);
      setState({ kind: "queued" });
    } catch (error) {
      setState({
        kind: "failed",
        message: error instanceof ApiError ? error.message : "The request failed.",
      });
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={() => void collect()}
        disabled={state.kind === "pending"}
        className={secondaryClass}
      >
        {state.kind === "pending" ? "Queuing…" : "Collect now"}
      </button>
      <p aria-live="polite" className="text-xs text-slate-600">
        {state.kind === "queued" && "Queued. Results appear here when the collection finishes."}
        {state.kind === "failed" && <span className="text-red-800">{state.message}</span>}
      </p>
    </div>
  );
}

/** The configured GitLab URL for links, only for users who may read settings; otherwise null. */
export function useGitLabUrl(): string | null {
  const { session } = useAuth();
  const allowed = session.status === "signed_in" && canManage(session.user.permissions);
  const settings = useLoad(`gitlab-url:${String(allowed)}`, async (signal) => {
    if (!allowed) return null;
    try {
      return (await getSettings(signal)).gitlab?.url ?? null;
    } catch {
      return null;
    }
  });
  return settings.data ?? null;
}

export function EnabledCell({ enabled }: { enabled: boolean | null }) {
  if (enabled === true) return <span>Enabled</span>;
  if (enabled === false) return <span className="text-amber-900">Disabled</span>;
  return <span className="text-slate-600">Unknown</span>;
}

export function ExternalLink({ href, children }: { href: string; children: string }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-sky-800 underline">
      {children}
      <span className="sr-only"> (opens in a new tab)</span> ↗
    </a>
  );
}
