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
  type KindFreshness,
} from "../api/gitlab";
import { effectivePolicies } from "../api/policies";
import {
  getCoverage,
  policyTypes,
  type CoverageRow,
} from "../api/securityPolicies";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { ErrorMessage } from "../components/ErrorMessage";
import { linkClass } from "../components/styles";
import { outcomeLabels, reasonHelp, severityLabels } from "../findings/format";
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
import { formatTime } from "../time";
import { ItemTags } from "../manager/parts";

type Kind = "group" | "project";

const cell = "px-3 py-2";
const card = "space-y-3 rounded border border-slate-200 bg-white p-4";

const outcomeClass: Record<string, string> = {
  pass: "bg-emerald-100 text-emerald-900",
  fail: "bg-red-100 text-red-900",
  unknown: "bg-amber-100 text-amber-900",
  unsupported: "bg-amber-100 text-amber-900",
  excepted: "bg-slate-200 text-slate-800",
};

function useGitlabId(): number | null {
  const { gitlabId = "" } = useParams();
  const id = Number(gitlabId);
  return Number.isInteger(id) && id > 0 ? id : null;
}

function NotFound({ kind }: { kind: Kind }) {
  return (
    <section aria-labelledby="page-title" className="space-y-3">
      <h1 id="page-title" className="text-2xl font-semibold">
        {kind === "group" ? "Group not found" : "Project not found"}
      </h1>
      <p className="text-slate-700">
        {`Baluarte hasn't collected this ${kind}. It may be new since the last collection, or outside what Baluarte's token can see.`}
      </p>
      <Link to="/inventory" className={linkClass}>
        Back to the inventory
      </Link>
    </section>
  );
}

function Loading({ what }: { what: string }) {
  return (
    <p role="status" className="text-sm text-slate-600">
      {`Loading ${what}…`}
    </p>
  );
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd>{children}</dd>
    </>
  );
}

/** The groups above, from the top down, each linked to its own page. */
function Breadcrumb({ ancestry, kind }: { ancestry: Ancestry; kind: Kind }) {
  const groups = [...ancestry.groups].reverse();
  return (
    <nav
      aria-label={`Groups above this ${kind}`}
      className="text-sm text-slate-600"
    >
      <Link to="/inventory" className={linkClass}>
        Inventory
      </Link>
      {groups.map((group) => (
        <span key={group.gitlab_id}>
          <span aria-hidden="true">{" / "}</span>
          <Link to={groupHref(group.gitlab_id)} className={linkClass}>
            {group.full_path}
          </Link>
        </span>
      ))}
    </nav>
  );
}

function AncestryNote({ ancestry, kind }: { ancestry: Ancestry; kind: Kind }) {
  if (ancestry.status === "complete") return null;
  const gaps = ancestry.gaps
    .map(
      (gap) =>
        `group ${String(gap.gitlab_id)} is ${gapReasons[gap.reason] ?? gap.reason}`,
    )
    .join("; ");
  return (
    <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
      {`Baluarte can't follow every group above this ${kind}${gaps ? ` (${gaps})` : ""}. What applies from above may be missing, so dependent results are unknown, not passing.`}
    </p>
  );
}

function CollectionFact({ collection }: { collection: KindFreshness }) {
  return (
    <Fact term="Collection">
      {statusLabel("collection", collection.status)}
      {collection.last_success_at && (
        <span className="text-slate-600">
          {`, last successful ${formatTime(collection.last_success_at)}`}
        </span>
      )}
      {collection.running && (
        <span className="text-slate-600">, collecting now</span>
      )}
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
    <header className="space-y-3">
      <Breadcrumb ancestry={ancestry} kind={kind} />
      <div>
        <p className="text-sm font-medium text-slate-600">
          {kind === "group" ? "GitLab group" : "GitLab project"}
        </p>
        <h1 id="page-title" className="text-2xl font-semibold break-all">
          {path}
        </h1>
      </div>
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[max-content_1fr]">
        {children}
      </dl>
      <AncestryNote ancestry={ancestry} kind={kind} />
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
      <h2 id="in-effect" className="text-lg font-semibold">
        Baselines in effect
      </h2>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : loaded.data ? (
        <ResolutionView resolution={loaded.data} />
      ) : (
        <Loading what="baselines" />
      )}
    </section>
  );
}

function ResultRow({ item, gitlabId }: { item: Evaluation; gitlabId: number }) {
  const reasons = item.unknown_reasons.map(
    (reason) => reasonHelp[reason] ?? reason,
  );
  return (
    <tr>
      <th scope="row" className={`${cell} font-medium`}>
        <code className="font-mono text-xs">{item.rule_id}</code>
        <span className="block text-xs font-normal text-slate-600">
          {item.rule_type}
        </span>
      </th>
      <td className={cell}>
        <span
          className={`rounded px-2 py-0.5 text-xs font-medium ${outcomeClass[item.outcome] ?? ""}`}
        >
          {outcomeLabels[item.outcome]}
        </span>
      </td>
      <td className={cell}>{severityLabels[item.severity] ?? item.severity}</td>
      <td className={cell}>
        {item.reason}
        {reasons.map((reason) => (
          <span key={reason} className="block text-slate-600">
            {reason}
          </span>
        ))}
      </td>
      <td className={`${cell} whitespace-nowrap`}>
        {formatTime(item.evaluated_at)}
      </td>
      <td className={cell}>
        {item.outcome === "pass" ? (
          <span className="text-slate-500">—</span>
        ) : (
          <Link
            to={findingsHref({
              project: gitlabId,
              policy: item.policy.name,
              rule: item.rule_id,
            })}
            className={linkClass}
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
  const loaded = useLoad(
    `project-evaluations:${String(gitlabId)}@${asOf ?? "now"}`,
    (signal) => getProjectEvaluations(gitlabId, signal, asOf),
  );
  let body: ReactNode;
  if (loaded.error)
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  else if (!loaded.data) body = <Loading what="results" />;
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
      <h2 id="results" className="text-lg font-semibold">
        Latest results
      </h2>
      {body}
    </section>
  );
}

function ResultTables({
  data,
  gitlabId,
}: {
  data: ProjectEvaluations;
  gitlabId: number;
}) {
  if (data.items.length === 0) {
    return (
      <p className="text-sm text-slate-600">
        No current results. The project is checked at the next evaluation if a
        baseline is in effect here.
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
      <div key={label} className="space-y-1">
        <h3 className="text-sm font-semibold">
          <Link to={baselineHref(name, version)} className={linkClass}>
            {label}
          </Link>
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] text-left text-sm">
            <caption className="sr-only">{`Results for ${label}`}</caption>
            <thead className="border-b border-slate-200 bg-slate-50">
              <tr>
                {[
                  "Rule",
                  "Result",
                  "Severity",
                  "Why",
                  "Evaluated",
                  "Findings",
                ].map((header) => (
                  <th
                    key={header}
                    scope="col"
                    className={`${cell} font-medium`}
                  >
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
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
    <ul className="list-disc space-y-1 pl-5 text-sm">
      {listed.map(({ type, policy }) => (
        <li key={`${type}-${String(policy.id)}-${policy.name}`}>
          {policy.id === null ? (
            policy.name
          ) : (
            <Link to={securityPolicyHref(policy.id)} className={linkClass}>
              {policy.name}
            </Link>
          )}
          <span className="text-slate-600">
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
function SecurityPolicies({
  kind,
  gitlabId,
}: {
  kind: Kind;
  gitlabId: number;
}) {
  const loaded = useLoad(`coverage:${kind}`, (signal) =>
    getCoverage(signal, kind),
  );
  let body: ReactNode;
  if (loaded.error)
    body = <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  else if (!loaded.data) body = <Loading what="GitLab security policies" />;
  else {
    const coverage = loaded.data;
    const rows = kind === "group" ? coverage.groups : coverage.projects;
    const row = rows.find((item) => item.gitlab_id === gitlabId);
    const groups = new Map(coverage.groups.map((item) => [item.path, item]));
    const problem = discoveryProblem(coverage);
    body = (
      <>
        {problem && (
          <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
            {problem}
          </p>
        )}
        {row ? (
          <>
            <p className="text-sm">
              <span className="font-medium">{coverageLabels[row.status]}</span>
              {`: ${coverageReason(row, kind, groups)}`}
            </p>
            {row.security_policy_project && (
              <p className="text-sm text-slate-600">
                {`Policy source project linked here: ${row.security_policy_project}`}
              </p>
            )}
            <PoliciesReaching row={row} />
          </>
        ) : (
          !problem && (
            <p className="text-sm text-slate-600">
              {`Discovery hasn't recorded this ${kind} yet, so its coverage is unknown, not empty.`}
            </p>
          )
        )}
      </>
    );
  }
  return (
    <section aria-labelledby="security-policies" className={card}>
      <h2 id="security-policies" className="text-lg font-semibold">
        GitLab security policies reaching this
      </h2>
      <p className="text-sm text-slate-600">
        GitLab enforces these itself; Baluarte only reads them.
      </p>
      {body}
      <Link to={COVERAGE_VIEW} className={`${linkClass} text-sm`}>
        All coverage
      </Link>
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
    <p
      role="status"
      className="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm"
    >
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
  const loaded = useLoad(
    `project:${String(gitlabId)}@${asOf ?? "now"}`,
    (signal) =>
      gitlabId === null
        ? Promise.reject(new ApiError(404, "Not found"))
        : getProject(gitlabId, signal, asOf),
  );
  const scope = useScopeChange(loaded.retry);
  if (gitlabId === null || isNotFound(loaded.error))
    return <NotFound kind="project" />;
  if (loaded.error)
    return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Loading what="project" />;
  const { project, ancestry, collection, not_evaluated } = loaded.data;
  const excluded =
    project.monitoring?.excluded === true || not_evaluated === "excluded";
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <Header
        kind="project"
        path={project.path_with_namespace}
        ancestry={ancestry}
      >
        <Fact term="GitLab ID">{project.gitlab_id}</Fact>
        <Fact term="State">
          <span title={stateHelp[project.state]}>
            {stateLabels[project.state]}
          </span>
        </Fact>
        <Fact term="Visibility">{visibilityLabel(project.visibility)}</Fact>
        <Fact term="Default branch">
          {project.default_branch ?? "None (empty repository)"}
        </Fact>
        {project.archived && (
          <Fact term="Archived">
            <span title="Archived projects are not checked by baselines.">
              Yes
            </span>
          </Fact>
        )}
        {project.namespace_kind === "user" && (
          <Fact term="Namespace">Personal</Fact>
        )}
        {project.tags && project.tags.length > 0 && (
          <Fact term="Tags">
            <ItemTags tags={project.tags} />
          </Fact>
        )}
        <Fact term="Last seen">{formatTime(project.last_seen_at)}</Fact>
        {project.last_error && (
          <Fact term="Last error">
            <span className="text-red-800">{project.last_error}</span>
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
        <p className="text-sm text-slate-700">
          Results and monitoring are shown as of the date viewed; baselines in
          effect and GitLab security policies are shown as they are now.
        </p>
      )}
      <p className="text-sm">
        <Link
          to={changesHref({ project: project.gitlab_id })}
          className={linkClass}
        >
          Changes to the default branch
        </Link>
      </p>
      <InEffect kind="project" gitlabId={project.gitlab_id} />
      {not_evaluated === "archived" || excluded ? (
        <section aria-labelledby="results" className={card}>
          <h2 id="results" className="text-lg font-semibold">
            Latest results
          </h2>
          <p className="text-sm text-slate-600">
            {excluded ? excludedNotice : archivedNotice}
          </p>
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
    gitlabId === null
      ? Promise.reject(new ApiError(404, "Not found"))
      : getGroup(gitlabId, signal),
  );
  const scope = useScopeChange(loaded.retry);
  if (gitlabId === null || isNotFound(loaded.error))
    return <NotFound kind="group" />;
  if (loaded.error)
    return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) return <Loading what="group" />;
  const { group, ancestry, collection, subgroups, projects } = loaded.data;
  const inventorySearch = `/inventory?${new URLSearchParams({ search: group.full_path }).toString()}`;
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <Header kind="group" path={group.full_path} ancestry={ancestry}>
        <Fact term="GitLab ID">{group.gitlab_id}</Fact>
        <Fact term="State">
          <span title={stateHelp[group.state]}>{stateLabels[group.state]}</span>
        </Fact>
        <Fact term="Visibility">{visibilityLabel(group.visibility)}</Fact>
        <Fact term="Contains">
          {`${String(subgroups)} ${subgroups === 1 ? "subgroup" : "subgroups"}, ${String(projects)} ${projects === 1 ? "project" : "projects"} directly. `}
          <Link to={inventorySearch} className={linkClass}>
            Find them in the inventory
          </Link>
        </Fact>
        {group.tags && group.tags.length > 0 && (
          <Fact term="Tags">
            <ItemTags tags={group.tags} />
          </Fact>
        )}
        <Fact term="Last seen">{formatTime(group.last_seen_at)}</Fact>
        {group.last_error && (
          <Fact term="Last error">
            <span className="text-red-800">{group.last_error}</span>
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
