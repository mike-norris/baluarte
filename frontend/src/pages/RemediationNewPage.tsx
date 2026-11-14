import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  CAN_PLAN,
  DEFAULT_PLAN_HOURS,
  MAX_PLAN_HOURS,
  MAX_PLAN_STEPS,
  PlanError,
  createPlan,
  type PlanScope,
  type SkippedFinding,
} from "../api/remediation";
import { useAuth } from "../auth/context";
import { PastReadOnly } from "../asof/PastReadOnly";
import { DryRunNotice } from "../remediation/DryRunNotice";
import { SkippedTable } from "../remediation/SkippedTable";
import { hasPermission, parseFindingIds, readPlanScope } from "../remediation/format";
import { CapabilityGate } from "../ui/CapabilityGate";
import { PageHeader } from "../ui/PageHeader";

const MAX_NAME = 63;

interface Failure {
  messages: string[];
  skipped: SkippedFinding[];
}

/** The scope and expiry from the form, or the problems with them. */
function readForm(form: FormData): { scope: PlanScope; hours: number } | { problems: string[] } {
  const text = (field: string) => {
    const value = form.get(field);
    return typeof value === "string" ? value.trim() : "";
  };
  const problems: string[] = [];
  const ids = parseFindingIds(text("findings"));
  const project = text("project");
  const policy = text("policy");
  const rule = text("rule");
  const filtered = Boolean(project || policy || rule);

  if (ids === null) problems.push("Finding IDs must be positive whole numbers.");
  else if (ids.length > MAX_PLAN_STEPS) {
    problems.push(`A plan covers at most ${String(MAX_PLAN_STEPS)} findings.`);
  }
  if (ids && ids.length > 0 && filtered) {
    problems.push("Give either finding IDs or filters, not both.");
  }
  if ((ids === null || ids.length === 0) && !filtered) {
    problems.push("Give finding IDs, or at least one of project, policy and rule.");
  }
  const projectId = Number(project);
  if (project && (!/^\d+$/.test(project) || !Number.isSafeInteger(projectId) || projectId <= 0)) {
    problems.push("The project GitLab ID must be a positive whole number.");
  }
  if (policy.length > MAX_NAME) problems.push("The policy name is at most 63 characters.");
  if (rule.length > MAX_NAME) problems.push("The rule ID is at most 63 characters.");
  const hours = Number(text("hours"));
  if (!Number.isInteger(hours) || hours < 1 || hours > MAX_PLAN_HOURS) {
    problems.push(`The plan must expire within 1 to ${String(MAX_PLAN_HOURS)} hours.`);
  }
  if (problems.length > 0) return { problems };

  const scope: PlanScope = {};
  if (ids && ids.length > 0) scope.finding_ids = ids;
  if (project) scope.project_gitlab_id = projectId;
  if (policy) scope.policy = policy;
  if (rule) scope.rule = rule;
  return { scope, hours };
}

function PlanForm({ csrfToken }: { csrfToken: string }) {
  const { refresh } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const initial = readPlanScope(params);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [sending, setSending] = useState(false);
  const alert = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (failure) alert.current?.focus();
  }, [failure]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const read = readForm(new FormData(event.currentTarget));
    if ("problems" in read) {
      setFailure({ messages: read.problems, skipped: [] });
      return;
    }
    setSending(true);
    setFailure(null);
    try {
      const plan = await createPlan(read.scope, read.hours, csrfToken);
      await navigate(`/remediation/${String(plan.id)}`, { state: { created: true } });
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure({
        messages: [apiError.message],
        skipped: apiError instanceof PlanError ? apiError.skipped : [],
      });
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-labelledby="plan-form"
      noValidate
      className="card"
    >
      <div className="card-header">
        <h2 id="plan-form" className="card-title">
          Scope
        </h2>
      </div>
      <div className="card-body">
        <p className="text-secondary">
          {`Give finding IDs, or filters that select every open finding they match. A plan covers at most ${String(MAX_PLAN_STEPS)} findings; a larger scope is refused. Only open findings whose latest result is a failure backed by current evidence become steps; the rest are listed as skipped with the reason.`}
        </p>
        {failure && (
          <div ref={alert} role="alert" tabIndex={-1} className="alert alert-danger text-body">
            <ul className="mb-0 ps-3">
              {failure.messages.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
            {failure.skipped.length > 0 && (
              <div className="mt-3">
                <p className="mb-2">Why each finding in scope was skipped:</p>
                <SkippedTable skipped={failure.skipped} caption="Findings that cannot be planned" />
              </div>
            )}
          </div>
        )}
        <label className="form-label w-100 mb-3">
          Finding IDs
          <input
            name="findings"
            defaultValue={(initial.finding_ids ?? []).join(", ")}
            aria-describedby="findings-help"
            className="form-control mt-1"
          />
          <span id="findings-help" className="form-hint fw-normal">
            Separated by commas or spaces. Leave empty to use the filters below.
          </span>
        </label>
        <fieldset className="row g-2 mb-3">
          <legend className="form-label col-12 mb-0">Or filters</legend>
          <label className="form-label col-sm-4 mb-0">
            Project GitLab ID
            <input
              name="project"
              inputMode="numeric"
              defaultValue={initial.project_gitlab_id ?? ""}
              className="form-control mt-1"
            />
          </label>
          <label className="form-label col-sm-4 mb-0">
            Policy name
            <input
              name="policy"
              defaultValue={initial.policy ?? ""}
              maxLength={MAX_NAME}
              className="form-control mt-1"
            />
          </label>
          <label className="form-label col-sm-4 mb-0">
            Rule ID
            <input
              name="rule"
              defaultValue={initial.rule ?? ""}
              maxLength={MAX_NAME}
              className="form-control mt-1"
            />
          </label>
        </fieldset>
        <label className="form-label d-block col-sm-4 mb-0">
          Expires after (hours)
          <input
            name="hours"
            type="number"
            min={1}
            max={MAX_PLAN_HOURS}
            step={1}
            defaultValue={DEFAULT_PLAN_HOURS}
            className="form-control mt-1"
          />
        </label>
      </div>
      <div className="card-footer btn-list justify-content-end">
        <Link to="/remediation" className="btn">
          Cancel
        </Link>
        <button type="submit" disabled={sending} className="btn btn-primary">
          {sending ? "Building…" : "Build dry-run plan"}
        </button>
      </div>
    </form>
  );
}

export function RemediationNewPage() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to="/remediation">All remediation plans</Link>}
        title="Create a dry-run plan"
        subtitle="The plan is built from stored evidence and starts as a draft. You submit it, and someone else approves or rejects it. Plans are built only while the projects collection is current."
      />
      <DryRunNotice />
      {user && hasPermission(user, CAN_PLAN) ? (
        <CapabilityGate capability="write" fallback={<PastReadOnly />}>
          <PlanForm csrfToken={user.csrf_token} />
        </CapabilityGate>
      ) : (
        <p className="text-secondary">
          Creating plans needs the remediation planning permission, which admins and analysts have.
          You can still read existing plans.
        </p>
      )}
    </section>
  );
}
