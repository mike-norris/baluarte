import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  findingsHref as filteredFindingsHref,
  getEvaluationSummary,
  type EvaluationSummary,
} from "../api/evaluations";
import {
  assignPolicy,
  getVersion,
  listAllAssignments,
  listVersions,
  type Assignment,
  type VersionSummary,
} from "../api/policies";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { buttonClass, linkClass, secondaryClass } from "../components/styles";
import { ApplyForm, appliedMessage } from "../policies/AssignForm";
import { AppliedToList } from "../policies/AssignmentList";
import {
  baselineHref,
  canWritePolicies,
  editorHref,
  findingsHref,
  GOVERNANCE,
  reviewText,
  targetText,
} from "../policies/format";
import { ResultLine, ResultsAsOf, total } from "../policies/results";
import { readableRules, scopeGroups, severityLabels } from "../policies/yaml";
import { formatTime } from "../time";
import type { SavedState } from "./BaselineEditorPage";

const emptyCounts = {
  pass: 0,
  fail: 0,
  unknown: 0,
  unsupported: 0,
  excepted: 0,
};

const severityClass: Record<string, string> = {
  critical: "bg-red-100 text-red-900",
  high: "bg-orange-100 text-orange-900",
  medium: "bg-amber-100 text-amber-900",
  low: "bg-slate-100 text-slate-800",
};

function VersionDetail({
  name,
  version,
  results,
}: {
  name: string;
  version: number;
  results: EvaluationSummary | undefined;
}) {
  const loaded = useLoad(`policy:${name}@${String(version)}`, (signal) =>
    getVersion(name, version, signal),
  );
  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (!loaded.data) {
    return (
      <p role="status" className="text-slate-600">
        Loading version…
      </p>
    );
  }
  const policy = loaded.data;
  const rules = readableRules(policy.document);
  const groups = scopeGroups(policy.document);
  const counted = results?.baselines
    .find((baseline) => baseline.name === name)
    ?.versions.find((item) => item.version === policy.version);
  const ruleCounts = new Map(counted?.rules.map((rule) => [rule.rule_id, rule.results]));
  return (
    <section
      aria-labelledby="rules"
      className="space-y-3 rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="rules" className="text-lg font-semibold">
        {`Rules in version ${String(policy.version)}`}
      </h2>
      {results && (
        <div className="space-y-1 text-sm">
          {counted && total(counted.projects) > 0 ? (
            <p>
              <ResultLine
                counts={counted.projects}
                failingHref={filteredFindingsHref({
                  state: "open",
                  policy: name,
                })}
                noun={["project checked", "projects checked"]}
              />
            </p>
          ) : (
            <p className="text-slate-600">
              No project was checked against this version in the latest evaluation.
            </p>
          )}
          <ResultsAsOf summary={results} />
        </div>
      )}
      {rules.length === 0 ? (
        <p className="text-sm text-slate-600">The rules are shown in the YAML below.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {rules.map((rule) => (
            <li key={rule.id} className="flex flex-wrap items-start gap-3 py-2 text-sm">
              <span
                className={`rounded px-2 py-0.5 text-xs font-medium ${severityClass[rule.severity] ?? ""}`}
              >
                {severityLabels[rule.severity]}
              </span>
              <span className="grow">
                <span className="font-medium">{rule.sentence}</span>
                {rule.detail && <span className="block text-slate-600">{rule.detail}</span>}
                {ruleCounts.get(rule.id) && (
                  <span className="block" aria-label={`Results for ${rule.id}`}>
                    <ResultLine
                      counts={ruleCounts.get(rule.id) ?? emptyCounts}
                      failingHref={filteredFindingsHref({
                        state: "open",
                        policy: name,
                        rule: rule.id,
                      })}
                    />
                  </span>
                )}
              </span>
              <code className="font-mono text-xs text-slate-600">{rule.id}</code>
            </li>
          ))}
        </ul>
      )}
      <p className="text-sm text-slate-600">
        {groups === null
          ? "Covers every group and project it is applied to."
          : `Covers only ${groups.join(", ")} and their subgroups, even where it is applied more widely.`}
      </p>
      <p className="text-sm text-slate-600">{`Review: ${reviewText(policy.review)}.`}</p>
      <details className="text-sm">
        <summary className="cursor-pointer font-medium">Show YAML (read-only)</summary>
        <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr]">
          <dt className="font-medium text-slate-700">Saved</dt>
          <dd>{formatTime(policy.created_at)}</dd>
          <dt className="font-medium text-slate-700">SHA-256</dt>
          <dd className="font-mono text-xs break-all">{policy.yaml_digest}</dd>
        </dl>
        <pre
          aria-label={`YAML of ${name} version ${String(policy.version)}`}
          className="mt-2 overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100"
        >
          {policy.source_yaml}
        </pre>
      </details>
    </section>
  );
}

function Versions({
  name,
  versions,
  selected,
}: {
  name: string;
  versions: VersionSummary[];
  selected: number;
}) {
  return (
    <nav aria-label="Versions" className="space-y-2">
      <h2 className="text-lg font-semibold">Versions</h2>
      <p className="text-sm text-slate-600">
        Saved versions never change, so every result names exactly what it was checked against.
      </p>
      <ul className="flex flex-wrap gap-2">
        {versions.map((version, index) => (
          <li key={version.version}>
            <Link
              to={baselineHref(name, version.version)}
              aria-current={version.version === selected ? "page" : undefined}
              className={`block rounded px-3 py-1.5 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 ${
                version.version === selected
                  ? "bg-slate-900 text-white"
                  : "border border-slate-300 bg-white text-slate-800 hover:bg-slate-100"
              }`}
            >
              {`v${String(version.version)}${index === 0 ? " (latest)" : ""}`}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** Offers to move every place on an older version to the latest one. */
function RolloutBanner({
  name,
  latest,
  behind,
  onDone,
}: {
  name: string;
  latest: number;
  behind: Assignment[];
  onDone: (message: string) => void;
}) {
  const { session, refresh } = useAuth();
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);

  async function rollOut() {
    if (session.status !== "signed_in") return;
    setSending(true);
    const results: string[] = [];
    for (const assignment of behind) {
      try {
        const applied = await assignPolicy(
          {
            policy_name: name,
            version: latest,
            target_type: assignment.target_type,
            ...(assignment.target_type === "instance" ? {} : { target_id: assignment.target_id }),
          },
          session.user.csrf_token,
        );
        results.push(appliedMessage(applied));
      } catch (error) {
        const apiError =
          error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
        results.push(
          `Applying v${String(latest)} to ${targetText(assignment)} failed: ${apiError.message}`,
        );
        if (apiError.status === 401) void refresh();
      }
    }
    setSending(false);
    setConfirming(false);
    onDone(results.join(" "));
  }

  const places = behind.length === 1 ? "1 place" : `${String(behind.length)} places`;
  return (
    <div className="space-y-2 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
      <p>{`v${String(latest)} is newer than the version applied in ${places}.`}</p>
      {confirming ? (
        <>
          <p>{`Apply v${String(latest)} to: ${behind.map(targetText).join("; ")}?`}</p>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={sending}
              onClick={() => void rollOut()}
              className={buttonClass}
            >
              {sending ? "Applying…" : `Apply v${String(latest)}`}
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
              }}
              className={secondaryClass}
            >
              Not now
            </button>
          </div>
        </>
      ) : (
        <button
          type="button"
          onClick={() => {
            setConfirming(true);
          }}
          className={secondaryClass}
        >
          {`Review and apply v${String(latest)}…`}
        </button>
      )}
    </div>
  );
}

export function BaselinePage() {
  const { name = "" } = useParams();
  const [params] = useSearchParams();
  const location = useLocation();
  const { session } = useAuth();
  const canWrite = canWritePolicies(session.status === "signed_in" ? session.user : null);
  const [reload, setReload] = useState(0);
  const [message, setMessage] = useState<string | null>(
    (location.state as SavedState | null)?.message ?? null,
  );
  const [applying, setApplying] = useState(false);
  const status = useRef<HTMLParagraphElement>(null);
  const versions = useLoad(`policy-versions:${name}`, (signal) => listVersions(name, signal));
  const assignments = useLoad(`policy-assignments:${name}#${String(reload)}`, (signal) =>
    listAllAssignments(signal, name),
  );
  const results = useLoad(`evaluation-summary:${name}`, (signal) =>
    getEvaluationSummary(name, signal),
  );
  const requested = Number(params.get("version"));
  const latest = versions.data?.[0]?.version;
  const selected = Number.isInteger(requested) && requested > 0 ? requested : latest;

  useEffect(() => {
    if (message) status.current?.focus();
  }, [message]);

  function changed(text: string) {
    setMessage(text);
    setApplying(false);
    setReload((value) => value + 1);
  }

  let body;
  if (versions.error) body = <ErrorMessage error={versions.error} retry={versions.retry} />;
  else if (!versions.data || selected === undefined || latest === undefined) {
    body = (
      <p role="status" className="text-slate-600">
        Loading baseline…
      </p>
    );
  } else {
    const applied = assignments.data ?? [];
    const behind = applied.filter((assignment) => assignment.version < latest);
    body = (
      <>
        {canWrite && behind.length > 0 && (
          <RolloutBanner name={name} latest={latest} behind={behind} onDone={changed} />
        )}
        {applying && (
          <ApplyForm
            name={name}
            versions={versions.data}
            onApplied={(result) => {
              changed(appliedMessage(result));
            }}
            onCancel={() => {
              setApplying(false);
            }}
          />
        )}
        <section aria-labelledby="applied" className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 id="applied" className="text-lg font-semibold">
              Applied to
            </h2>
            {applied.length > 0 && (
              <Link to={findingsHref(name)} className={linkClass}>
                Open findings for this baseline
              </Link>
            )}
          </div>
          {assignments.error ? (
            <ErrorMessage error={assignments.error} retry={assignments.retry} />
          ) : !assignments.data ? (
            <p role="status" className="text-slate-600">
              Loading where it is applied…
            </p>
          ) : (
            <AppliedToList assignments={applied} latest={latest} onChanged={changed} />
          )}
        </section>
        <VersionDetail name={name} version={selected} results={results.data} />
        <Versions name={name} versions={versions.data} selected={selected} />
      </>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm">
            <Link to={GOVERNANCE} className={linkClass}>
              Governance
            </Link>
            {" › Baselines"}
          </p>
          <h1 id="page-title" className="mt-1 text-2xl font-semibold">
            {name}
          </h1>
        </div>
        {canWrite && selected !== undefined && (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => {
                setApplying(true);
                setMessage(null);
              }}
              className={buttonClass}
            >
              Apply…
            </button>
            <Link to={editorHref({ name, version: selected })} className={secondaryClass}>
              Edit
            </Link>
          </div>
        )}
      </div>
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="text-sm font-medium">
          {message}
        </p>
      )}
      {body}
    </section>
  );
}
