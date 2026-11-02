import { Link, useParams } from "react-router";

import {
  getSecurityPolicy,
  type PolicyScope,
  type ReachedTarget,
  type SecurityPolicyDetail,
} from "../api/securityPolicies";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { linkClass } from "../components/styles";
import { SECURITY_POLICIES_VIEW } from "../policies/format";
import { summarizePolicy, typeLabels } from "../security-policies/format";
import { GovernanceHeader } from "../policies/GovernanceTabs";
import {
  DiscoveryNotice,
  EnabledCell,
  ExternalLink,
  INTRO,
  useGitLabUrl,
} from "../security-policies/parts";
import { formatTime } from "../time";

const SHOWN = 25;

function scopeLines(scope: PolicyScope): string[] {
  const lines: string[] = [];
  const list = (label: string, items: string[]) => {
    if (items.length > 0) lines.push(`${label}: ${items.join(", ")}`);
  };
  list("Only projects", scope.including_projects);
  list("Excludes projects", scope.excluding_projects);
  list("Only groups", scope.including_groups);
  list("Excludes groups", scope.excluding_groups);
  list("Only compliance frameworks", scope.compliance_frameworks);
  if (scope.excluding_archived_projects) lines.push("Excludes archived projects");
  if (scope.excluding_personal_projects) lines.push("Excludes personal projects");
  return lines;
}

function Reached({
  title,
  targets,
  kind,
}: {
  title: string;
  targets: ReachedTarget[];
  kind: "group" | "project";
}) {
  return (
    <div>
      <h3 className="font-medium">{`${title} (${String(targets.length)})`}</h3>
      {targets.length === 0 ? (
        <p className="text-sm text-slate-600">{`No ${kind} lists this policy in its latest discovery.`}</p>
      ) : (
        <ul className="mt-1 space-y-0.5 text-sm">
          {targets.slice(0, SHOWN).map((target) => (
            <li key={target.id}>
              {target.path}
              <span className="text-slate-600">
                {target.inherited ? ` · inherited${target.via ? ` from ${target.via}` : ""}` : ""}
              </span>
            </li>
          ))}
          {targets.length > SHOWN && (
            <li className="text-slate-600">{`and ${String(targets.length - SHOWN)} more`}</li>
          )}
        </ul>
      )}
    </div>
  );
}

function Detail({ policy }: { policy: SecurityPolicyDetail }) {
  const gitlab = useGitLabUrl();
  const summary = summarizePolicy(policy.policy_type, policy.yaml);
  const scope = scopeLines(policy.scope);

  return (
    <article aria-labelledby="policy-title" className="space-y-5">
      <div className="space-y-1">
        <p className="text-sm">
          <Link to={SECURITY_POLICIES_VIEW} className={linkClass}>
            GitLab security policies
          </Link>
        </p>
        <h2 id="policy-title" className="text-xl font-semibold">
          {policy.name}
        </h2>
        <p className="text-sm text-slate-700">
          {`${typeLabels[policy.policy_type]} · `}
          <EnabledCell enabled={policy.enabled} />
          {policy.state === "missing" && " · No longer found in GitLab"}
          <span className="text-slate-600"> · from GitLab, read-only</span>
        </p>
        {policy.description && (
          <p className="max-w-3xl text-sm text-slate-600">{policy.description}</p>
        )}
      </div>

      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
        <dt className="font-medium">Policy source project</dt>
        <dd>
          {policy.origin_path}
          {policy.origin_kind === "linked_source" && (
            <span className="block text-slate-600">
              Baluarte&apos;s token can&apos;t see the source project itself; this is the group or
              project it is linked to.
            </span>
          )}
          {gitlab && policy.origin_kind === "policy_project" && (
            <span className="ml-2">
              <ExternalLink href={`${gitlab}/${policy.origin_path}`}>Open in GitLab</ExternalLink>
            </span>
          )}
        </dd>
        <dt className="font-medium">Linked to</dt>
        <dd>
          {policy.linked_from.length > 0
            ? `${policy.linked_from.map((link) => link.path).join(", ")} (and what is under them)`
            : "Not known"}
        </dd>
        <dt className="font-medium">Scope</dt>
        <dd>
          {scope.length > 0 ? (
            <ul>
              {scope.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          ) : (
            "No exclusions"
          )}
          {policy.scope.truncated && (
            <span className="block text-amber-900">
              The scope lists were cut short, so where it applies may be incomplete.
            </span>
          )}
        </dd>
        <dt className="font-medium">Seen</dt>
        <dd>
          {`First ${formatTime(policy.first_seen_at)}, last ${formatTime(policy.last_seen_at)}`}
          {policy.gitlab_updated_at &&
            `; changed in GitLab ${formatTime(policy.gitlab_updated_at)}`}
        </dd>
      </dl>

      <section
        aria-labelledby="summary-title"
        className="space-y-2 rounded border border-slate-200 bg-white p-4"
      >
        <h3 id="summary-title" className="font-semibold">
          What it does
        </h3>
        {summary ? (
          <>
            {summary.lines.length > 0 ? (
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {summary.lines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-slate-600">Nothing to summarise; see the YAML below.</p>
            )}
            {summary.notable.map((item) => (
              <p
                key={item.text}
                className={`rounded p-2 text-sm ${
                  item.level === "warn"
                    ? "border border-amber-300 bg-amber-50 text-amber-950"
                    : "bg-slate-50"
                }`}
              >
                {item.level === "warn" && <span aria-hidden="true">⚠ </span>}
                {item.text}
              </p>
            ))}
            <p className="text-xs text-slate-600">
              A reading of the YAML below, which is what GitLab enforces.
            </p>
          </>
        ) : (
          <p className="text-sm text-slate-600">
            {policy.yaml
              ? "Baluarte can't summarise this policy's YAML; read it below."
              : "GitLab returned no YAML for this policy."}
          </p>
        )}
      </section>

      <section aria-labelledby="reach-title" className="space-y-3">
        <h3 id="reach-title" className="font-semibold">
          Where it reaches
        </h3>
        <div className="grid gap-4 sm:grid-cols-2">
          <Reached title="Groups" targets={policy.reached.groups} kind="group" />
          <Reached title="Projects" targets={policy.reached.projects} kind="project" />
        </div>
      </section>

      {policy.yaml && (
        <details className="rounded border border-slate-200 bg-white p-4">
          <summary className="cursor-pointer font-medium">
            YAML (read-only, as stored in GitLab)
          </summary>
          <pre className="mt-3 overflow-x-auto rounded bg-slate-50 p-3 text-xs">
            <code>{policy.yaml}</code>
          </pre>
          {policy.yaml_digest && (
            <p className="mt-2 text-xs break-all text-slate-600">{`SHA-256 ${policy.yaml_digest}`}</p>
          )}
        </details>
      )}
    </article>
  );
}

export function SecurityPolicyPage() {
  const { policyId = "" } = useParams();
  const id = Number(policyId);
  const valid = Number.isInteger(id) && id > 0;
  const policy = useLoad(`security-policy:${policyId}`, (signal) =>
    valid ? getSecurityPolicy(id, signal) : Promise.reject(new Error("invalid id")),
  );

  let body;
  if (!valid || policy.error?.status === 404) {
    body = (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {"This GitLab security policy isn't known to Baluarte. "}
        <Link to={SECURITY_POLICIES_VIEW} className={linkClass}>
          See all GitLab security policies
        </Link>
        .
      </p>
    );
  } else if (policy.error) body = <ErrorMessage error={policy.error} retry={policy.retry} />;
  else if (!policy.data) {
    body = (
      <p role="status" className="text-slate-600">
        Loading GitLab security policy…
      </p>
    );
  } else {
    body = (
      <>
        <DiscoveryNotice context={policy.data} />
        <Detail policy={policy.data} />
      </>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <GovernanceHeader intro={INTRO} />
      {body}
    </section>
  );
}
