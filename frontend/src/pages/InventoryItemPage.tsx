/**
 * One group or project from the inventory: what Baluarte collected, which baselines are in effect
 * and why, a project's latest result for each rule, and the GitLab security policies reaching it.
 */

import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router";

import { changesHref } from "../api/changes";
import { ApiError } from "../api/client";
import {
  findingsHref,
  getProjectEvaluations,
  type Evaluation,
  type ProjectEvaluations,
} from "../api/evaluations";
import {
  getGroup,
  getProject,
  groupHref,
  type Ancestry,
  type InventoryState,
  type KindFreshness,
} from "../api/gitlab";
import { effectivePolicies } from "../api/policies";
import { getCoverage, policyTypes, type CoverageRow } from "../api/securityPolicies";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { ErrorMessage } from "../components/ErrorMessage";
import { OutcomeBadge, SeverityBadge } from "../findings/badges";
import { reasonHelp } from "../findings/format";
import { statusLabel } from "../freshness/status";
import { ExclusionNotice } from "../inventory/ExclusionNotice";
import { stateHelp, stateLabels, visibilityLabel } from "../inventory/format";
import { ResolutionView } from "../policies/EffectivePanel";
import { baselineHref, gapReasons } from "../policies/format";
import {
  COVERAGE_VIEW,
  coverageLabels,
  coverageReason,
  discoveryProblem,
  securityPolicyHref,
  typeLabels,
} from "../security-policies/format";
import { ItemTags } from "../manager/parts";
import { formatTime } from "../time";
import { badgeClass, type Tone } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

type Kind = "group" | "project";

const card = "card card-body mb-3";

const stateTone: Record<InventoryState, Tone> = {
  active: "success",
  inaccessible: "warning",
  missing: "danger",
};

function useGitlabId(): number | null {
  const { gitlabId = "" } = useParams();
  const id = Number(gitlabId);
  return Number.isInteger(id) && id > 0 ? id : null;
}

function NotFound({ kind }: { kind: Kind }) {
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to="/inventory">Inventory</Link>}
        title={kind === "group" ? "Group not found" : "Project not found"}
      />
      <EmptyState title={`Baluarte hasn't collected this ${kind}.`}>
        <p className="mb-2">
          It may be new since the last collection, or outside what Baluarte&apos;s token can see.
        </p>
        <Link to="/inventory">Back to the inventory</Link>
      </EmptyState>
    </section>
  );
}

function Waiting({ what }: { what: string }) {
  return (
    <section aria-labelledby="page-title">
      <PageHeader pretitle={<Link to="/inventory">Inventory</Link>} title={what} />
      <Loading label={`Loading ${what.toLowerCase()}…`} />
    </section>
  );
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="col-sm-4 col-lg-3 fw-medium text-secondary">{term}</dt>
      <dd className="col-sm-8 col-lg-9 mb-1 text-break">{children}</dd>
    </>
  );
}

/** The groups above, from the top down, each linked to its own page. */
function Breadcrumb({ ancestry, kind }: { ancestry: Ancestry; kind: Kind }) {
  const groups = [...ancestry.groups].reverse();
  return (
    <nav aria-label={`Groups above this ${kind}`} className="mb-2">
      <ol className="breadcrumb">
        <li className="breadcrumb-item">
          <Link to="/inventory">Inventory</Link>
        </li>
        {groups.map((group) => (
          <li key={group.gitlab_id} className="breadcrumb-item">
            <Link to={groupHref(group.gitlab_id)}>{group.full_path}</Link>
          </li>
        ))}
      </ol>
    </nav>
  );
}

function AncestryNote({ ancestry, kind }: { ancestry: Ancestry; kind: Kind }) {
  if (ancestry.status === "complete") return null;
  const gaps = ancestry.gaps
    .map((gap) => `group ${String(gap.gitlab_id)} is ${gapReasons[gap.reason] ?? gap.reason}`)
    .join("; ");
  return (
    <p className="alert alert-warning text-body">
      {`Baluarte can't follow every group above this ${kind}${gaps ? ` (${gaps})` : ""}. What applies from above may be missing, so dependent results are unknown, not passing.`}
    </p>
  );
}

function CollectionFact({ collection }: { collection: KindFreshness }) {
  return (
    <Fact term="Collection">
      {statusLabel("collection", collection.status)}
      {collection.last_success_at && (
        <span className="text-secondary">
          {`, last successful ${formatTime(collection.last_success_at)}`}
        </span>
      )}
      {collection.running && <span className="text-secondary">, collecting now</span>}
    </Fact>
  );
}

function Header({
  kind,
  path,
  ancestry,
  children,
}: {
  kind: Kind;
  path: string;
  ancestry: Ancestry;
  children: ReactNode;
}) {
  return (
    <header>
      <Breadcrumb ancestry={ancestry} kind={kind} />
      <PageHeader
        pretitle={kind === "group" ? "GitLab group" : "GitLab project"}
        title={<span className="text-break">{path}</span>}
      />
      <AncestryNote ancestry={ancestry} kind={kind} />
      <section aria-label="Details" className="card card-body mb-3">
        <dl className="row mb-0">{children}</dl>
      </section>
    </header>
  );
}

/** Baselines in effect here, with the chain they are inherited through. */
function InEffect({ kind, gitlabId }: { kind: Kind; gitlabId: number }) {
  const loaded = useLoad(`effective:${kind}:${String(gitlabId)}`, (signal) =>
    effectivePolicies(kind, gitlabId, signal),
  );
  return (
    <section aria-labelledby="in-effect" className={card}>
      <h2 id="in-effect" className="card-title">
        Baselines in effect
      </h2>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : loaded.data ? (
        <ResolutionView resolution={loaded.data} />
      ) : (
        <Loading label="Loading baselines…" />
      )}
    </section>
  );
}

function ResultRow({ item, gitlabId }: { item: Evaluation; gitlabId: number }) {
  const reasons = item.unknown_reasons.map((reason) => reasonHelp[reason] ?? reason);
  return (
    <tr>
      <th scope="row" data-label="Rule" className="fw-medium">
        <code>{item.rule_id}</code>
        <span className="d-block small fw-normal text-secondary">{item.rule_type}</span>
      </th>
      <td data-label="Result">
        <OutcomeBadge outcome={item.outcome} brief />
      </td>
      <td data-label="Severity" className="d-none d-md-table-cell">
        <SeverityBadge severity={item.severity} />
      </td>
      <td data-label="Why">
        {item.reason}
        {reasons.map((reason) => (
          <span key={reason} className="d-block text-secondary">
            {reason}
          </span>
        ))}
      </td>
      <td data-label="Evaluated" className="text-nowrap d-none d-md-table-cell">
        {formatTime(item.evaluated_at)}
      </td>
      <td data-label="Findings">
        {item.outcome === "pass" ? (
          <span className="text-secondary">—</span>
        ) : (
          <Link
            to={findingsHref({
              project: gitlabId,
              policy: item.policy.name,
              rule: item.rule_id,
            })}

            aria-label={`Findings for ${item.rule_id} from ${item.policy.name}`}
          >
            Findings
          </Link>
        )}
      </td>
    </tr>
  );
}

const archivedNotice =
  "Archived: not checked by baselines. Findings were resolved when it was archived.";
const excludedNotice =
  "Excluded from monitoring: not checked by baselines. Findings were resolved when it was excluded and reopen if it is included again and still fails.";

/** The latest result of each rule, grouped by the baseline version it came from. */
function Results({ gitlabId }: { gitlabId: number }) {
  const asOf = useAsOf();
  const loaded = useLoad(`project-evaluations:${String(gitlabId)}@${asOf ?? "now"}`, (signal) =>
    getProjectEvaluations(gitlabId, signal, asOf),
  );
  let body: ReactNode;
  if (loaded.error) body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  else if (!loaded.data) body = <Loading label="Loading results…" />;
  else {
    body = (
      <>
        <AsOfResult answer={loaded.data} />
        <ResultTables data={loaded.data} gitlabId={gitlabId} />
      </>
    );
  }
  return (
    <section aria-labelledby="results" className={card}>
      <h2 id="results" className="card-title">
        Latest results
      </h2>
      {body}
    </section>
  );
}

function ResultTables({ data, gitlabId }: { data: ProjectEvaluations; gitlabId: number }) {
  if (data.items.length === 0) {
    return (
      <p className="text-secondary">
        No current results. The project is checked at the next evaluation if a baseline is in effect
        here.
      </p>
    );
  }
  const byVersion = new Map<string, Evaluation[]>();
  for (const item of data.items) {
    const key = `${item.policy.name}@${String(item.policy.version)}`;
    byVersion.set(key, [...(byVersion.get(key) ?? []), item]);
  }
  return [...byVersion.values()].map((items) => {
    const first = items[0];
    if (!first) return null;
    const { name, version } = first.policy;
    const label = `${name} v${String(version)}`;
    return (
      <div key={label}>
        <h3 className="h4 mt-3">
          <Link to={baselineHref(name, version)}>{label}</Link>
        </h3>
        <div className="table-responsive-md">
          <table className="table table-vcenter table-mobile-md mb-0">
            <caption className="visually-hidden">{`Results for ${label}`}</caption>
            <thead>
              <tr>
                {["Rule", "Result", "Severity", "Why", "Evaluated", "Findings"].map((header) => (
                  <th
                    key={header}
                    scope="col"
                    className={
                      header === "Severity" || header === "Evaluated"
                        ? "d-none d-md-table-cell"
                        : ""
                    }
                  >
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <ResultRow key={item.id} item={item} gitlabId={gitlabId} />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  });
}

function PoliciesReaching({ row }: { row: CoverageRow }) {
  const listed = policyTypes.flatMap((type) =>
    (row.types[type]?.policies ?? []).map((policy) => ({ type, policy })),
  );
  if (listed.length === 0) return null;
  return (
    <ul className="ps-3">
      {listed.map(({ type, policy }) => (
        <li key={`${type}-${String(policy.id)}-${policy.name}`}>
          {policy.id === null ? (
            policy.name
          ) : (
            <Link to={securityPolicyHref(policy.id)}>{policy.name}</Link>
          )}
          <span className="text-secondary">
            {` — ${typeLabels[type]}, ${policy.enabled === false ? "disabled" : policy.enabled ? "enabled" : "enabled state unknown"}`}
            {policy.via && `, from ${policy.via}`}
            {policy.inherited && " (inherited)"}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** GitLab security policies reaching this group or project, from the coverage read. */
function SecurityPolicies({ kind, gitlabId }: { kind: Kind; gitlabId: number }) {
  const loaded = useLoad(`coverage:${kind}`, (signal) => getCoverage(signal, kind));
  let body: ReactNode;
  if (loaded.error) body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  else if (!loaded.data) body = <Loading label="Loading GitLab security policies…" />;
  else {
    const coverage = loaded.data;
    const rows = kind === "group" ? coverage.groups : coverage.projects;
    const row = rows.find((item) => item.gitlab_id === gitlabId);
    const groups = new Map(coverage.groups.map((item) => [item.path, item]));
    const problem = discoveryProblem(coverage);
    body = (
      <>
        {problem && <p className="alert alert-warning text-body">{problem}</p>}
        {row ? (
          <>
            <p>
              <span className="fw-medium">{coverageLabels[row.status]}</span>
              {`: ${coverageReason(row, kind, groups)}`}
            </p>
            {row.security_policy_project && (
              <p className="text-secondary">
                {`Policy source project linked here: ${row.security_policy_project}`}
              </p>
            )}
            <PoliciesReaching row={row} />
          </>
        ) : (
          !problem && (
            <p className="text-secondary">
              {`Discovery hasn't recorded this ${kind} yet, so its coverage is unknown, not empty.`}
            </p>
          )
        )}
      </>
    );
  }
  return (
    <section aria-labelledby="security-policies" className={card}>
      <h2 id="security-policies" className="card-title">
        GitLab security policies reaching this
      </h2>
      <p className="text-secondary">GitLab enforces these itself; Baluarte only reads them.</p>
      {body}
      <Link to={COVERAGE_VIEW}>All coverage</Link>
    </section>
  );
}

function isNotFound(error: ApiError | undefined): boolean {
  return error?.status === 404;
}

/** Monitoring changed from this page: say what happened and reload the page's facts. */
function useScopeChange(reload: () => void) {
  const [message, setMessage] = useState<string | null>(null);
  const notice = message ? (
    <p role="status" className="alert alert-success text-body">
      {message}
    </p>
  ) : null;
  return {
    notice,
    changed: (text: string) => {
      setMessage(text);
      reload();
    },
  };
}

export function ProjectPage() {
  const gitlabId = useGitlabId();
  const asOf = useAsOf();
  const loaded = useLoad(`project:${String(gitlabId)}@${asOf ?? "now"}`, (signal) =>
    gitlabId === null
      ? Promise.reject(new ApiError(404, "Not found"))
      : getProject(gitlabId, signal, asOf),
  );
  const scope = useScopeChange(loaded.retry);
  if (gitlabId === null || isNotFound(loaded.error)) return <NotFound kind="project" />;
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Waiting what="Project" />;
  const { project, ancestry, collection, not_evaluated } = loaded.data;
  const excluded = project.monitoring?.excluded === true || not_evaluated === "excluded";
  return (
    <section aria-labelledby="page-title">
      <Header kind="project" path={project.path_with_namespace} ancestry={ancestry}>
        <Fact term="GitLab ID">{project.gitlab_id}</Fact>
        <Fact term="State">
          <span className={badgeClass(stateTone[project.state])} title={stateHelp[project.state]}>
            {stateLabels[project.state]}
          </span>
        </Fact>
        <Fact term="Visibility">{visibilityLabel(project.visibility)}</Fact>
        <Fact term="Default branch">{project.default_branch ?? "None (empty repository)"}</Fact>
        {project.archived && (
          <Fact term="Archived">
            <span title="Archived projects are not checked by baselines.">Yes</span>
          </Fact>
        )}
        {project.namespace_kind === "user" && <Fact term="Namespace">Personal</Fact>}
        {project.tags && project.tags.length > 0 && (
          <Fact term="Tags">
            <ItemTags tags={project.tags} />
          </Fact>
        )}
        <Fact term="Last seen">{formatTime(project.last_seen_at)}</Fact>
        {project.last_error && (
          <Fact term="Last error">
            <span className="text-danger-emphasis">{project.last_error}</span>
          </Fact>
        )}
        <CollectionFact collection={collection} />
      </Header>
      <ExclusionNotice
        monitoring={project.monitoring}
        type="project"
        path={project.path_with_namespace}
        onChanged={scope.changed}
      />
      {scope.notice}
      {asOf && (
        <p className="text-secondary">
          Results and monitoring are shown as of the date viewed; baselines in effect and GitLab
          security policies are shown as they are now.
        </p>
      )}
      <p>
        <Link to={changesHref({ project: project.gitlab_id })}>Changes to the default branch</Link>
      </p>
      <InEffect kind="project" gitlabId={project.gitlab_id} />
      {not_evaluated === "archived" || excluded ? (
        <section aria-labelledby="results" className={card}>
          <h2 id="results" className="card-title">
            Latest results
          </h2>
          <p className="text-secondary">{excluded ? excludedNotice : archivedNotice}</p>
        </section>
      ) : (
        <Results gitlabId={project.gitlab_id} />
      )}
      <SecurityPolicies kind="project" gitlabId={project.gitlab_id} />
    </section>
  );
}

export function GroupPage() {
  const gitlabId = useGitlabId();
  const loaded = useLoad(`group:${String(gitlabId)}`, (signal) =>
    gitlabId === null ? Promise.reject(new ApiError(404, "Not found")) : getGroup(gitlabId, signal),
  );
  const scope = useScopeChange(loaded.retry);
  if (gitlabId === null || isNotFound(loaded.error)) return <NotFound kind="group" />;
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Waiting what="Group" />;
  const { group, ancestry, collection, subgroups, projects } = loaded.data;
  const inventorySearch = `/inventory?${new URLSearchParams({ search: group.full_path }).toString()}`;
  return (
    <section aria-labelledby="page-title">
      <Header kind="group" path={group.full_path} ancestry={ancestry}>
        <Fact term="GitLab ID">{group.gitlab_id}</Fact>
        <Fact term="State">
          <span className={badgeClass(stateTone[group.state])} title={stateHelp[group.state]}>
            {stateLabels[group.state]}
          </span>
        </Fact>
        <Fact term="Visibility">{visibilityLabel(group.visibility)}</Fact>
        <Fact term="Contains">
          {`${String(subgroups)} ${subgroups === 1 ? "subgroup" : "subgroups"}, ${String(projects)} ${projects === 1 ? "project" : "projects"} directly. `}
          <Link to={inventorySearch}>Find them in the inventory</Link>
        </Fact>
        {group.tags && group.tags.length > 0 && (
          <Fact term="Tags">
            <ItemTags tags={group.tags} />
          </Fact>
        )}
        <Fact term="Last seen">{formatTime(group.last_seen_at)}</Fact>
        {group.last_error && (
          <Fact term="Last error">
            <span className="text-danger-emphasis">{group.last_error}</span>
          </Fact>
        )}
        <CollectionFact collection={collection} />
      </Header>
      <ExclusionNotice
        monitoring={group.monitoring}
        type="group"
        path={group.full_path}
        onChanged={scope.changed}
      />
      {scope.notice}
      <InEffect kind="group" gitlabId={group.gitlab_id} />
      <SecurityPolicies kind="group" gitlabId={group.gitlab_id} />
    </section>
  );
}
