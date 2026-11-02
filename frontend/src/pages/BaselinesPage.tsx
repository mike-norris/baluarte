import { Link } from "react-router";

import { getEvaluationSummary, type EvaluationSummary } from "../api/evaluations";
import {
  listAllAssignments,
  listPolicies,
  type Assignment,
  type PolicySummary,
} from "../api/policies";
import { listSecurityPolicies } from "../api/securityPolicies";
import { useLoad, type Loaded } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, linkClass, secondaryClass } from "../components/styles";
import {
  baselineHref,
  canWritePolicies,
  editorHref,
  findingsHref,
  SECURITY_POLICIES_VIEW,
} from "../policies/format";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import { ResultLine, ResultsAsOf, total } from "../policies/results";
import { readableRules, recommendedBaseline, severityLabels, toDocument } from "../policies/yaml";

const cell = "px-3 py-2";
const SHOWN_TARGETS = 3;

function placeName(assignment: Assignment): string {
  if (assignment.target_type === "instance") return "Whole instance";
  return assignment.target_path ?? `GitLab ID ${String(assignment.target_id)}`;
}

function AppliedCell({ policy, applied }: { policy: PolicySummary; applied: Assignment[] }) {
  if (applied.length === 0) return <span className="text-slate-600">Not applied</span>;
  const behind = applied.some((assignment) => assignment.version < policy.latest_version);
  return (
    <>
      <ul className="space-y-0.5">
        {applied.slice(0, SHOWN_TARGETS).map((assignment) => (
          <li key={assignment.id}>
            {`${placeName(assignment)} `}
            <span className="text-slate-600">{`(v${String(assignment.version)})`}</span>
          </li>
        ))}
      </ul>
      {applied.length > SHOWN_TARGETS && (
        <p className="text-slate-600">{`and ${String(applied.length - SHOWN_TARGETS)} more`}</p>
      )}
      {behind && (
        <p className="text-xs text-amber-900">{`v${String(policy.latest_version)} isn't applied everywhere yet`}</p>
      )}
    </>
  );
}

/** Projects failing, passing and unknown for a baseline, from the latest evaluation. */
function ResultsCell({
  name,
  applied,
  results,
}: {
  name: string;
  applied: boolean;
  results: Loaded<EvaluationSummary>;
}) {
  if (!applied) return <span className="text-slate-600">—</span>;
  const counts = results.data?.baselines.find((baseline) => baseline.name === name)?.projects;
  if (counts && total(counts) > 0) {
    return (
      <ResultLine counts={counts} failingHref={findingsHref(name)} noun={["project", "projects"]} />
    );
  }
  return (
    <>
      <span className="block text-slate-600">
        {results.error ? "Counts unavailable" : results.data ? "Not checked yet" : "Loading…"}
      </span>
      <Link to={findingsHref(name)} className={linkClass}>
        Open findings
      </Link>
    </>
  );
}

function BaselineTable({
  policies,
  assignments,
  results,
}: {
  policies: PolicySummary[];
  assignments: Assignment[];
  results: Loaded<EvaluationSummary>;
}) {
  return (
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Baselines</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            {["Baseline", "Latest version", "Applied to", "Results"].map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 align-top">
          {policies.map((policy) => {
            const applied = assignments.filter((item) => item.policy_name === policy.name);
            return (
              <tr key={policy.name}>
                <th scope="row" className={`${cell} font-medium`}>
                  <Link to={baselineHref(policy.name)} className={linkClass}>
                    {policy.name}
                  </Link>
                  <span className="block text-xs font-normal text-slate-600">
                    {`${String(policy.versions)} ${policy.versions === 1 ? "version" : "versions"}`}
                  </span>
                </th>
                <td className={cell}>{`v${String(policy.latest_version)}`}</td>
                <td className={cell}>
                  <AppliedCell policy={policy} applied={applied} />
                </td>
                <td className={cell}>
                  <ResultsCell name={policy.name} applied={applied.length > 0} results={results} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** "We also found N GitLab security policies": shown only when discovery found some. */
function FoundSecurityPolicies() {
  // Optional context for the first run, so a failed read hides the line instead of an error.
  const found = useLoad("security-policies:found", (signal) =>
    listSecurityPolicies({ policyType: "", includeMissing: false }, null, signal).catch(() => null),
  );
  const page = found.data;
  if (!page || page.items.length === 0) return null;
  const count = `${String(page.items.length)}${page.next_after_id !== null ? "+" : ""}`;
  return (
    <p className="rounded border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
      {`We also found ${count} GitLab security ${page.items.length === 1 ? "policy" : "policies"} on this instance. They are enforced by GitLab, not Baluarte. `}
      <Link to={SECURITY_POLICIES_VIEW} className={linkClass}>
        View them
      </Link>
    </p>
  );
}

/** The first-run state: what a baseline is, and the recommended one to start from. */
function GetStarted({ canWrite }: { canWrite: boolean }) {
  const recommended = readableRules(toDocument(recommendedBaseline()));
  return (
    <section
      aria-labelledby="get-started"
      className="space-y-4 rounded border border-slate-200 bg-white p-5"
    >
      <h2 id="get-started" className="text-lg font-semibold">
        Baluarte isn&apos;t checking anything yet
      </h2>
      <p className="max-w-3xl text-sm text-slate-600">
        A baseline is a set of rules Baluarte checks your GitLab configuration against, for example
        &quot;projects are not public&quot;. Baluarte only reads GitLab; it never changes it.
      </p>
      {canWrite ? (
        <>
          <div className="max-w-2xl rounded border border-slate-200 p-4">
            <h3 className="font-semibold">Recommended baseline</h3>
            <ul className="mt-2 space-y-1 text-sm">
              {recommended.map((rule) => (
                <li key={rule.id} className="flex justify-between gap-4">
                  <span>{rule.sentence}</span>
                  <span className="text-slate-600">{severityLabels[rule.severity]}</span>
                </li>
              ))}
            </ul>
            <div className="mt-4 flex flex-wrap gap-2">
              <Link to={editorHref("recommended")} className={buttonClass}>
                Use the recommended baseline
              </Link>
            </div>
            <p className="mt-2 text-sm text-slate-600">
              You review the rules and choose where to apply them before anything is saved.
            </p>
          </div>
          <p className="text-sm">
            {"Or "}
            <Link to={editorHref()} className={linkClass}>
              create a baseline
            </Link>
            {" from scratch, or "}
            <Link to={`${editorHref()}?mode=yaml`} className={linkClass}>
              import YAML
            </Link>
            .
          </p>
        </>
      ) : (
        <p className="text-sm text-slate-600">
          No baselines yet. An administrator or analyst can create one.
        </p>
      )}
      <FoundSecurityPolicies />
    </section>
  );
}

export function BaselinesPage() {
  const { session } = useAuth();
  const canWrite = canWritePolicies(session.status === "signed_in" ? session.user : null);
  const policies = useLoad("policies", listPolicies);
  const assignments = useLoad("policy-assignments:all", (signal) => listAllAssignments(signal));
  // Counts are extra: without them the table still shows, with a link to findings.
  const results = useLoad("evaluation-summary", (signal) => getEvaluationSummary(null, signal));

  let body;
  if (policies.error) body = <ErrorMessage error={policies.error} retry={policies.retry} />;
  else if (assignments.error) {
    body = <ErrorMessage error={assignments.error} retry={assignments.retry} />;
  } else if (!policies.data || !assignments.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading baselines…
      </p>
    );
  } else if (policies.data.length === 0) {
    body = <GetStarted canWrite={canWrite} />;
  } else {
    body = (
      <div className="space-y-3">
        {assignments.data.length === 0 && (
          <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
            No baseline is applied anywhere, so nothing is checked. Open a baseline and apply it.
          </p>
        )}
        <BaselineTable policies={policies.data} assignments={assignments.data} results={results} />
        {results.data && <ResultsAsOf summary={results.data} />}
      </div>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <GovernanceHeader
        intro="Baselines are rules Baluarte checks against your GitLab configuration. Apply a baseline to the whole instance, a group or a project; closer settings replace broader ones."
        action={
          canWrite && policies.data && policies.data.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              <Link to={editorHref()} className={buttonClass}>
                Create a baseline
              </Link>
              <Link to={`${editorHref()}?mode=yaml`} className={secondaryClass}>
                Import YAML
              </Link>
            </div>
          ) : undefined
        }
      />
      {body}
      <p className="rounded border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
        {
          "GitLab security policies, such as scan execution and merge request approval policies, are enforced by GitLab itself, not by Baluarte. "
        }
        <Link to={SECURITY_POLICIES_VIEW} className={linkClass}>
          About GitLab security policies
        </Link>
      </p>
    </section>
  );
}
