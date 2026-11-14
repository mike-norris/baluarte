import type { ReactNode } from "react";
import { Link, useParams } from "react-router";

import { ApiError } from "../api/client";
import { controlPath } from "../api/controls";
import {
  getFinding,
  getFindingHistory,
  getObservation,
  type Evaluation,
  type EvaluationPage,
  type FindingDetail,
} from "../api/evaluations";
import { projectHref } from "../api/gitlab";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { FailingChanges } from "../changes/FailingChanges";
import { ErrorMessage } from "../components/ErrorMessage";
import { frameworkLabel } from "../controls/format";
import { ExportLink } from "../evidence/ExportLink";
import { evidenceHref } from "../evidence/scope";
import { ExceptionPanel } from "../exceptions/ExceptionPanel";
import { EvaluationNote } from "../findings/EvaluationNote";
import { writePlanScope } from "../remediation/format";
import { PlanLink } from "../remediation/PlanLink";
import { FindingStateBadge, OutcomeBadge, SeverityBadge } from "../findings/badges";
import {
  capabilityLabels,
  findingStateLabels,
  formatValue,
  isStructured,
  outcomeLabels,
  outcomeMeaning,
  reasonHelp,
  resolutionLabels,
  severityLabels,
} from "../findings/format";
import { observedSummary } from "../findings/observed";
import { formatTime } from "../time";
import { useDeviceClass } from "../ui/device";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

const warningClass = "alert alert-danger text-body";

/** Whether the evaluation holds an observed value that was actually judged. */
function observedKnown(evaluation: Evaluation): boolean {
  const decided = evaluation.outcome === "pass" || evaluation.outcome === "fail";
  return decided && evaluation.observed !== null && evaluation.observed !== undefined;
}

function Value({ value }: { value: unknown }) {
  const text = formatValue(value);
  return isStructured(value) ? (
    <pre className="mt-1 mb-0 small">{text}</pre>
  ) : (
    <span className="font-monospace">{text}</span>
  );
}

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="row g-2 py-2 border-bottom">
      <dt className="col-md-4 fw-medium text-secondary">{term}</dt>
      <dd className="col-md-8 mb-0">{children}</dd>
    </div>
  );
}

function Summary({ finding }: { finding: FindingDetail }) {
  const evaluation = finding.latest_evaluation;
  return (
    <section aria-labelledby="summary" className="card card-body mb-3">
      <h2 id="summary" className="card-title">
        Summary
      </h2>
      <dl className="mb-0">
        <Field term="State">
          {findingStateLabels[finding.state]}
          {finding.state === "excepted" &&
            finding.exception &&
            ` until ${formatTime(finding.exception.expires_at)} (not counted as passing)`}
          {finding.state === "resolved" && finding.resolved_at && (
            <>
              {` on ${formatTime(finding.resolved_at)}`}
              {finding.resolution &&
                `, because ${resolutionLabels[finding.resolution] ?? finding.resolution}`}
            </>
          )}
        </Field>
        <Field term="Severity">{severityLabels[finding.severity] ?? finding.severity}</Field>
        <Field term="Latest outcome">
          {`${outcomeLabels[finding.latest_outcome]}: ${outcomeMeaning[finding.latest_outcome]}`}
        </Field>
        <Field term="Policy">
          {`${finding.policy.name}, version ${String(finding.policy.version)}`}
          {" (the pinned version evaluated)"}
        </Field>
        <Field term="Rule">
          {finding.rule_id} <span className="text-secondary">({evaluation.rule_type})</span>
        </Field>
        <Field term="Project">
          <Link to={projectHref(finding.project.gitlab_id)}>{finding.project.path}</Link>{" "}
          <span className="text-secondary">
            {`(GitLab ID ${String(finding.project.gitlab_id)}, ${finding.project.state})`}
          </span>
        </Field>
        {finding.controls && (
          <Field term="Controls">
            {finding.controls.length === 0 ? (
              "Not mapped to any control."
            ) : (
              <ul className="d-flex flex-wrap column-gap-3 row-gap-1" aria-label="Controls">
                {finding.controls.map((control) => (
                  <li key={`${control.framework}:${control.id}`}>
                    <Link to={controlPath(control.framework, control.id)}>
                      {`${frameworkLabel(control.framework)} ${control.id}`}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Field>
        )}
        <Field term="First seen">{formatTime(finding.first_seen_at)}</Field>
        <Field term="Last seen">{formatTime(finding.last_seen_at)}</Field>
      </dl>
    </section>
  );
}

function ExpectedVersusActual({ evaluation }: { evaluation: Evaluation }) {
  const known = observedKnown(evaluation);
  const summary = known ? observedSummary(evaluation.rule_type, evaluation.observed) : null;
  const { capability, collection } = evaluation;
  const capabilityText = capability.name
    ? `${capability.name}: ${capabilityLabels[capability.status] ?? capability.status}`
    : "None required";
  const collectionText =
    `${collection.kind}: ${collection.status}; ` +
    `last success ${formatTime(collection.last_success_at)}`;
  return (
    <section aria-labelledby="comparison" className="card card-body mb-3">
      <h2 id="comparison" className="card-title">
        Expected versus actual
      </h2>
      <p className="mt-1 text-secondary">
        {`Latest evaluation, ${formatTime(evaluation.evaluated_at)}.`}
      </p>
      <dl className="row g-3 mt-1">
        <div className="col-sm-6">
          <div className="border rounded p-3 h-100">
            <dt className="fw-medium text-secondary">Expected</dt>
            <dd className="mt-1">
              <Value value={evaluation.expected} />
            </dd>
          </div>
        </div>
        <div className="col-sm-6">
          <div className="border rounded p-3 h-100">
            <dt className="fw-medium text-secondary">Observed</dt>
            <dd className="mt-1">
              {known ? (
                <>
                  {summary && <p>{summary}</p>}
                  <Value value={evaluation.observed} />
                </>
              ) : (
                <span>{`Unknown — ${evaluation.reason}`}</span>
              )}
            </dd>
          </div>
        </div>
      </dl>
      <p className="mt-3">
        <span className="fw-medium">{`Outcome: ${outcomeLabels[evaluation.outcome]}. `}</span>
        {outcomeMeaning[evaluation.outcome]}
      </p>
      {known && <p className="mt-1 text-secondary">{evaluation.reason}</p>}
      {evaluation.unknown_reasons.length > 0 && (
        <>
          <h3 className="mt-3 h4">Why it could not be decided</h3>
          <ol className="mt-1 ps-3">
            {evaluation.unknown_reasons.map((code, index) => (
              <li key={code}>
                <code className="small">{code}</code>
                {`: ${reasonHelp[code]}`}
                {index === 0 && evaluation.unknown_reasons.length > 1 && " (explains the result)"}
              </li>
            ))}
          </ol>
        </>
      )}
      <h3 className="mt-3 h4">Context</h3>
      <dl className="mb-0">
        <Field term="Capability">{capabilityText}</Field>
        <Field term="Collection at evaluation">{collectionText}</Field>
        <Field term="Policy inheritance">
          {evaluation.ancestry === "complete"
            ? "Complete: every group above the project was followed."
            : "Incomplete: a group above the project could not be followed."}
        </Field>
        <Field term="Source">
          <code className="small">{evaluation.source}</code>
        </Field>
        <Field term="Observed at">{formatTime(evaluation.observed_at)}</Field>
      </dl>
    </section>
  );
}

const integrityWarning =
  "The evidence this result judged failed its integrity check: its SHA-256 digest no longer matches its contents, so it may have been altered. The result was recorded as unknown.";

function EvidenceRecord({
  projectId,
  observationId,
  flagged,
}: {
  projectId: number;
  observationId: number;
  flagged: boolean;
}) {
  const loaded = useLoad(`observation:${String(projectId)}:${String(observationId)}`, (signal) =>
    getObservation(projectId, observationId, signal).catch((error: unknown) => {
      // The project left the inventory, so its evidence cannot be read by project.
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }),
  );

  if (loaded.error) return <ErrorMessage error={loaded.error} retry={loaded.retry} />;
  if (loaded.loading) {
    return (
      <p role="status" className="text-secondary">
        Loading evidence…
      </p>
    );
  }
  const observation = loaded.data;
  if (!observation) {
    return (
      <p role="alert" className={warningClass}>
        {`The linked evidence record (ID ${String(observationId)}) `}
        could not be found for this project.
      </p>
    );
  }
  const tampered = flagged || !observation.digest_valid;
  return (
    <>
      {tampered && (
        <p role="alert" className={warningClass}>
          {integrityWarning}
        </p>
      )}
      <dl className="mb-0">
        <Field term="Evidence ID">{String(observation.id)}</Field>
        <Field term="Source">
          <code className="small">{observation.source}</code>
        </Field>
        <Field term="Reference">
          <code className="small">{observation.source_ref ?? "None"}</code>
        </Field>
        <Field term="Collected at">{formatTime(observation.collected_at)}</Field>
        <Field term="Collection status">{observation.collection_status}</Field>
        {observation.error && <Field term="Collection error">{observation.error}</Field>}
        <Field term="SHA-256 digest">
          <code className="text-break">{observation.payload_digest ?? "None (no payload)"}</code>
        </Field>
        <Field term="Integrity">
          {observation.digest_valid
            ? "Verified: the digest matches the stored contents."
            : "Failed: the digest does not match the stored contents."}
        </Field>
      </dl>
      {observation.payload && (
        <details className="mt-2">
          <summary className="fw-medium">Collected data</summary>
          <pre className="mt-1 mb-0 small">{JSON.stringify(observation.payload, null, 2)}</pre>
        </details>
      )}
    </>
  );
}

function Evidence({ evaluation }: { evaluation: Evaluation }) {
  const flagged = evaluation.unknown_reasons.includes("evidence_digest_mismatch");
  const decided = evaluation.outcome === "pass" || evaluation.outcome === "fail";
  return (
    <section aria-labelledby="evidence" className="card card-body mb-3">
      <h2 id="evidence" className="card-title">
        Evidence
      </h2>
      {evaluation.observation_id === null ? (
        <>
          {flagged && (
            <p role="alert" className={warningClass}>
              {integrityWarning}
            </p>
          )}
          <p role={decided ? "alert" : undefined} className={warningClass}>
            No evidence record is linked to this result, so it is not backed by collected data
            {decided ? "." : " and is not a compliance claim."}
          </p>
        </>
      ) : (
        <EvidenceRecord
          projectId={evaluation.project.gitlab_id}
          observationId={evaluation.observation_id}
          flagged={flagged}
        />
      )}
    </section>
  );
}

function History({ findingId }: { findingId: number }) {
  const asOf = useAsOf();
  const key = `history:${String(findingId)}@${asOf ?? "now"}`;
  const first = useLoad(key, (signal) => getFindingHistory(findingId, null, signal, asOf));
  const paged = usePaged<Evaluation, EvaluationPage>(
    key,
    first,
    (page) => page.next_before_id,
    (before) => getFindingHistory(findingId, before, undefined, asOf),
  );

  let body: ReactNode;
  if (first.error) body = <ErrorMessage error={first.error} retry={first.retry} />;
  else if (first.loading) {
    body = (
      <p role="status" className="text-secondary">
        Loading history…
      </p>
    );
  } else if (paged.items.length === 0) {
    body = <p className="text-secondary">No evaluations are recorded for this rule.</p>;
  } else {
    body = (
      <div>
        <div className="table-responsive">
          <table className="table table-vcenter card-table mb-0">
            <caption className="visually-hidden">Evaluation history, newest first</caption>
            <thead>
              <tr>
                {["Evaluated", "Outcome", "Policy version", "Observed", "Reason"].map((header) => (
                  <th key={header} scope="col">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paged.items.map((evaluation, index) => (
                <tr key={evaluation.id}>
                  <th scope="row" className="fw-normal text-nowrap">
                    {formatTime(evaluation.evaluated_at)}
                    {index === 0 && <span className="ms-1 text-secondary">(latest)</span>}
                  </th>
                  <td>
                    <OutcomeBadge outcome={evaluation.outcome} brief />
                  </td>
                  <td>{`v${String(evaluation.policy.version)}`}</td>
                  <td>
                    {observedKnown(evaluation)
                      ? (observedSummary(evaluation.rule_type, evaluation.observed) ??
                        formatValue(evaluation.observed))
                      : "Unknown"}
                  </td>
                  <td>{evaluation.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {paged.moreError && (
          <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
        )}
        {paged.next !== null && (
          <button
            type="button"
            onClick={() => void paged.loadMore()}
            disabled={paged.pending}
            className="btn"
          >
            {paged.pending ? "Loading…" : "Load older evaluations"}
          </button>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="history" className="card card-body mb-3">
      <h2 id="history" className="card-title">
        History
      </h2>
      <p className="mt-1 text-secondary">
        {asOf
          ? "Every evaluation of this rule for the project up to the date viewed, newest first."
          : "Every evaluation of this rule for the project, newest first, across policy versions."}
      </p>
      {body}
    </section>
  );
}

export function FindingDetailPage() {
  const { findingId = "" } = useParams();
  const asOf = useAsOf();
  const device = useDeviceClass();
  const id = Number(findingId);
  const valid = Number.isSafeInteger(id) && id > 0 && String(id) === findingId;
  const loaded = useLoad(`finding:${findingId}@${asOf ?? "now"}`, (signal) =>
    valid ? getFinding(id, signal, asOf) : Promise.reject(new ApiError(404, "No such finding")),
  );
  const pretitle = <Link to="/findings">Findings</Link>;

  if (loaded.error?.status === 404) {
    return (
      <section aria-labelledby="page-title">
        <PageHeader pretitle={pretitle} title="Finding not found" />
        <EmptyState title={`There is no finding with ID ${findingId}.`}>
          <Link to="/findings">Back to findings</Link>
        </EmptyState>
      </section>
    );
  }
  if (loaded.error || !loaded.data) {
    return (
      <section aria-labelledby="page-title">
        <PageHeader pretitle={pretitle} title="Finding" />
        {loaded.error ? (
          <ErrorMessage error={loaded.error} retry={loaded.retry} />
        ) : (
          <Loading label="Loading finding…" />
        )}
      </section>
    );
  }
  const finding = loaded.data;
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={pretitle}
        title={`${finding.rule_id} on ${finding.project.path}`}
        subtitle={`Finding ${String(finding.id)}`}
        status={
          <span className="d-inline-flex flex-wrap gap-1">
            <FindingStateBadge state={finding.state} />
            <SeverityBadge severity={finding.severity} />
            <OutcomeBadge outcome={finding.latest_outcome} />
          </span>
        }
      />
      {asOf ? <AsOfResult answer={finding} /> : <EvaluationNote />}
      <div className="row row-cards">
        <div className="col-lg-4 order-lg-2">
          <Summary finding={finding} />
          <ExceptionPanel finding={finding} />
          <div className="d-flex flex-column gap-1 mb-3">
            <ExportLink
              label="Export evidence for this project and rule"
              href={evidenceHref({
                projects: String(finding.project.gitlab_id),
                policy: finding.policy.name,
                rule: finding.rule_id,
              })}
            />
            {finding.state === "open" &&
              finding.latest_outcome === "fail" &&
              device !== "phone" && (
                <PlanLink
                  label="Plan a dry-run fix for this finding"
                  href={`/remediation/new?${writePlanScope({ finding_ids: [finding.id] }).toString()}`}
                />
              )}
          </div>
        </div>
        <div className="col-lg-8 order-lg-1">
          <ExpectedVersusActual evaluation={finding.latest_evaluation} />
          <FailingChanges evaluation={finding.latest_evaluation} />
          <Evidence evaluation={finding.latest_evaluation} />
          <History findingId={finding.id} />
        </div>
      </div>
    </section>
  );
}
