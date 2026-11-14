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
import { useDeviceClass } from "../ui/device";

const cell = "align-top";
const SHOWN_TARGETS = 3;

function placeName(assignment: Assignment): string {
  if (assignment.target_type === "instance") return "Whole instance";
  return assignment.target_path ?? `GitLab ID ${String(assignment.target_id)}`;
}

function AppliedCell({ policy, applied }: { policy: PolicySummary; applied: Assignment[] }) {
  if (applied.length === 0) return <span className="text-secondary">Not applied</span>;
  const behind = applied.some((assignment) => assignment.version < policy.latest_version);
  return (
    <>
      <ul>
        {applied.slice(0, SHOWN_TARGETS).map((assignment) => (
          <li key={assignment.id}>
            {`${placeName(assignment)} `}
            <span className="text-secondary">{`(v${String(assignment.version)})`}</span>
          </li>
        ))}
      </ul>
      {applied.length > SHOWN_TARGETS && (
        <p className="text-secondary">{`and ${String(applied.length - SHOWN_TARGETS)} more`}</p>
      )}
      {behind && (
        <p className="small text-warning-emphasis">{`v${String(policy.latest_version)} isn't applied everywhere yet`}</p>
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
  if (!applied) return <span className="text-secondary">—</span>;
  const counts = results.data?.baselines.find((baseline) => baseline.name === name)?.projects;
  if (counts && total(counts) > 0) {
    return (
      <ResultLine counts={counts} failingHref={findingsHref(name)} noun={["project", "projects"]} />
    );
  }
  return (
    <>
      <span className="d-block text-secondary">
        {results.error ? "Counts unavailable" : results.data ? "Not checked yet" : "Loading…"}
      </span>
      <Link to={findingsHref(name)}>Open findings</Link>
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
    <div className="card table-responsive">
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">Baselines</caption>
        <thead>
          <tr>
            {["Baseline", "Latest version", "Applied to", "Results"].map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="align-top">
          {policies.map((policy) => {
            const applied = assignments.filter((item) => item.policy_name === policy.name);
            return (
              <tr key={policy.name}>
                <th scope="row" className={`${cell} font-medium`}>
                  <Link to={baselineHref(policy.name)}>{policy.name}</Link>
                  <span className="d-block small fw-normal text-secondary">
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
    <p className="alert alert-info text-body">
      {`We also found ${count} GitLab security ${page.items.length === 1 ? "policy" : "policies"} on this instance. They are enforced by GitLab, not Baluarte. `}
      <Link to={SECURITY_POLICIES_VIEW}>View them</Link>
    </p>
  );
}

/** The first-run state: what a baseline is, and the recommended one to start from. */
function GetStarted({ canWrite }: { canWrite: boolean }) {
  const recommended = readableRules(toDocument(recommendedBaseline()));
  return (
    <section aria-labelledby="get-started" className="rounded border p-5">
      <h2 id="get-started" className="card-title">
        Baluarte isn&apos;t checking anything yet
      </h2>
      <p className="max-w-3xl text-secondary">
        A baseline is a set of rules Baluarte checks your GitLab configuration against, for example
        &quot;projects are not public&quot;. Baluarte only reads GitLab; it never changes it.
      </p>
      {canWrite ? (
        <>
          <div className="max-w-2xl rounded border p-3">
            <h3 className="fw-semibold">Recommended baseline</h3>
            <ul className="mt-2">
              {recommended.map((rule) => (
                <li key={rule.id} className="d-flex justify-content-between gap-3">
                  <span>{rule.sentence}</span>
                  <span className="text-secondary">{severityLabels[rule.severity]}</span>
                </li>
              ))}
            </ul>
            <div className="mt-3 d-flex flex-wrap gap-2">
              <Link to={editorHref("recommended")} className="btn btn-primary">
                Use the recommended baseline
              </Link>
            </div>
            <p className="mt-2 text-secondary">
              You review the rules and choose where to apply them before anything is saved.
            </p>
          </div>
          <p>
            {"Or "}
            <Link to={editorHref()}>create a baseline</Link>
            {" from scratch, or "}
            <Link to={`${editorHref()}?mode=yaml`}>import YAML</Link>.
          </p>
        </>
      ) : (
        <p className="text-secondary">
          No baselines yet. An administrator or analyst can create one.
        </p>
      )}
      <FoundSecurityPolicies />
    </section>
  );
}

export function BaselinesPage() {
  const { session } = useAuth();
  const device = useDeviceClass();
  // Phones read baselines; creating one needs a larger screen.
  const canWrite =
    canWritePolicies(session.status === "signed_in" ? session.user : null) && device !== "phone";
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
      <p role="status" className="text-secondary">
        Loading baselines…
      </p>
    );
  } else if (policies.data.length === 0) {
    body = <GetStarted canWrite={canWrite} />;
  } else {
    body = (
      <div>
        {assignments.data.length === 0 && (
          <p className="alert alert-warning text-body">
            No baseline is applied anywhere, so nothing is checked. Open a baseline and apply it.
          </p>
        )}
        <BaselineTable policies={policies.data} assignments={assignments.data} results={results} />
        {results.data && <ResultsAsOf summary={results.data} />}
      </div>
    );
  }

  return (
    <section aria-labelledby="page-title">
      <GovernanceHeader
        intro="Baselines are rules Baluarte checks against your GitLab configuration. Apply a baseline to the whole instance, a group or a project; closer settings replace broader ones."
        action={
          canWrite && policies.data && policies.data.length > 0 ? (
            <div className="d-flex flex-wrap gap-2">
              <Link to={editorHref()} className="btn btn-primary">
                Create a baseline
              </Link>
              <Link to={`${editorHref()}?mode=yaml`} className="btn">
                Import YAML
              </Link>
            </div>
          ) : undefined
        }
      />
      {body}
      <p className="alert alert-info text-body">
        {
          "GitLab security policies, such as scan execution and merge request approval policies, are enforced by GitLab itself, not by Baluarte. "
        }
        <Link to={SECURITY_POLICIES_VIEW}>About GitLab security policies</Link>
      </p>
    </section>
  );
}
