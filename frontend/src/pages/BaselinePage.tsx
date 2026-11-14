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
import { PageHeader } from "../ui/PageHeader";
import { useDeviceClass } from "../ui/device";

const emptyCounts = {
  pass: 0,
  fail: 0,
  unknown: 0,
  unsupported: 0,
  excepted: 0,
};

const severityClass: Record<string, string> = {
  critical: "bg-danger text-danger-fg",
  high: "bg-orange-100 text-orange-900",
  medium: "bg-warning-subtle text-warning-emphasis border border-warning-subtle",
  low: "bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle",
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
      <p role="status" className="text-secondary">
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
    <section aria-labelledby="rules" className="card card-body mb-3">
      <h2 id="rules" className="card-title">
        {`Rules in version ${String(policy.version)}`}
      </h2>
      {results && (
        <div>
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
            <p className="text-secondary">
              No project was checked against this version in the latest evaluation.
            </p>
          )}
          <ResultsAsOf summary={results} />
        </div>
      )}
      {rules.length === 0 ? (
        <p className="text-secondary">The rules are shown in the YAML below.</p>
      ) : (
        <ul>
          {rules.map((rule) => (
            <li key={rule.id} className="d-flex flex-wrap align-items-start gap-3 py-2">
              <span
                className={`rounded px-2 py-0.5 text-xs font-medium ${severityClass[rule.severity] ?? ""}`}
              >
                {severityLabels[rule.severity]}
              </span>
              <span className="grow">
                <span className="fw-medium">{rule.sentence}</span>
                {rule.detail && <span className="d-block text-secondary">{rule.detail}</span>}
                {ruleCounts.get(rule.id) && (
                  <span className="d-block" aria-label={`Results for ${rule.id}`}>
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
              <code className="font-monospace small text-secondary">{rule.id}</code>
            </li>
          ))}
        </ul>
      )}
      <p className="text-secondary">
        {groups === null
          ? "Covers every group and project it is applied to."
          : `Covers only ${groups.join(", ")} and their subgroups, even where it is applied more widely.`}
      </p>
      <p className="text-secondary">{`Review: ${reviewText(policy.review)}.`}</p>
      <details>
        <summary className="fw-medium">Show YAML (read-only)</summary>
        <dl className="mt-2 grid column-gap-3 row-gap-1 sm:grid-cols-[max-content_1fr]">
          <dt className="col-md-4 fw-medium text-secondary">Saved</dt>
          <dd>{formatTime(policy.created_at)}</dd>
          <dt className="col-md-4 fw-medium text-secondary">SHA-256</dt>
          <dd className="font-monospace small text-break">{policy.yaml_digest}</dd>
        </dl>
        <pre
          aria-label={`YAML of ${name} version ${String(policy.version)}`}
          className="mt-2 mb-0 small"
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
    <nav aria-label="Versions">
      <h2 className="card-title">Versions</h2>
      <p className="text-secondary">
        Saved versions never change, so every result names exactly what it was checked against.
      </p>
      <ul className="d-flex flex-wrap gap-2">
        {versions.map((version, index) => (
          <li key={version.version}>
            <Link
              to={baselineHref(name, version.version)}
              aria-current={version.version === selected ? "page" : undefined}
              className={`btn btn-sm ${version.version === selected ? "btn-primary" : ""}`}
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
    <div className="alert alert-warning text-body">
      <p>{`v${String(latest)} is newer than the version applied in ${places}.`}</p>
      {confirming ? (
        <>
          <p>{`Apply v${String(latest)} to: ${behind.map(targetText).join("; ")}?`}</p>
          <div className="d-flex gap-2">
            <button
              type="button"
              disabled={sending}
              onClick={() => void rollOut()}
              className="btn btn-primary"
            >
              {sending ? "Applying…" : `Apply v${String(latest)}`}
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
              }}
              className="btn"
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
          className="btn"
        >
          {`Review and apply v${String(latest)}…`}
        </button>
      )}
    </div>
  );
}

export function BaselinePage() {
  const device = useDeviceClass();
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
      <p role="status" className="text-secondary">
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
        <section aria-labelledby="applied">
          <div className="d-flex flex-wrap align-items-center justify-content-between gap-2">
            <h2 id="applied" className="card-title">
              Applied to
            </h2>
            {applied.length > 0 && (
              <Link to={findingsHref(name)}>Open findings for this baseline</Link>
            )}
          </div>
          {assignments.error ? (
            <ErrorMessage error={assignments.error} retry={assignments.retry} />
          ) : !assignments.data ? (
            <p role="status" className="text-secondary">
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
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={
          <>
            <Link to={GOVERNANCE}>Governance</Link>
            {" › Baselines"}
          </>
        }
        title={name}
        actions={
          canWrite && selected !== undefined && device !== "phone" ? (
            <>
              <button
                type="button"
                onClick={() => {
                  setApplying(true);
                  setMessage(null);
                }}
                className="btn btn-primary"
              >
                Apply…
              </button>
              <Link to={editorHref({ name, version: selected })} className="btn">
                Edit
              </Link>
            </>
          ) : undefined
        }
      />
      {message && (
        <p ref={status} role="status" tabIndex={-1} className="alert alert-success text-body">
          {message}
        </p>
      )}
      {body}
    </section>
  );
}
