import type { ReactNode } from "react";
import { Link, useParams } from "react-router";

import { ApiError } from "../api/client";
import {
  getFinding,
  getFindingHistory,
  getObservation,
  type Evaluation,
  type EvaluationPage,
  type FindingDetail,
} from "../api/evaluations";
import { usePaged } from "../api/usePaged";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { linkClass, secondaryClass } from "../components/styles";
import { ExportLink } from "../evidence/ExportLink";
import { evidenceHref } from "../evidence/scope";
import { ExceptionPanel } from "../exceptions/ExceptionPanel";
import { EvaluationNote } from "../findings/EvaluationNote";
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
import { formatTime } from "../time";

const cell = "px-3 py-2";
const warningClass = "rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900";

/** Whether the evaluation holds an observed value that was actually judged. */
function observedKnown(evaluation: Evaluation): boolean {
  const decided = evaluation.outcome === "pass" || evaluation.outcome === "fail";
  return decided && evaluation.observed !== null && evaluation.observed !== undefined;
}

function Value({ value }: { value: unknown }) {
  const text = formatValue(value);
  return isStructured(value) ? (
    <pre className="mt-1 overflow-x-auto rounded bg-slate-100 p-2 text-xs">{text}</pre>
  ) : (
    <span className="font-mono">{text}</span>
  );
}

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="py-2 sm:grid sm:grid-cols-3 sm:gap-4">
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="mt-1 sm:col-span-2 sm:mt-0">{children}</dd>
    </div>
  );
}

function inventoryHref(path: string): string {
  return `/inventory?${new URLSearchParams({ search: path }).toString()}`;
}

function Summary({ finding }: { finding: FindingDetail }) {
  const evaluation = finding.latest_evaluation;
  return (
    <section aria-labelledby="summary" className="rounded border border-slate-200 bg-white p-4">
      <h2 id="summary" className="text-lg font-semibold">
        Summary
      </h2>
      <dl className="mt-2 divide-y divide-slate-100 text-sm">
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
          {finding.rule_id} <span className="text-slate-600">({evaluation.rule_type})</span>
        </Field>
        <Field term="Project">
          <Link to={inventoryHref(finding.project.path)} className={linkClass}>
            {finding.project.path}
          </Link>{" "}
          <span className="text-slate-600">
            {`(GitLab ID ${String(finding.project.gitlab_id)}, ${finding.project.state})`}
          </span>
        </Field>
        <Field term="First seen">{formatTime(finding.first_seen_at)}</Field>
        <Field term="Last seen">{formatTime(finding.last_seen_at)}</Field>
      </dl>
    </section>
  );
}

function ExpectedVersusActual({ evaluation }: { evaluation: Evaluation }) {
  const known = observedKnown(evaluation);
  const { capability, collection } = evaluation;
  const capabilityText = capability.name
    ? `${capability.name}: ${capabilityLabels[capability.status] ?? capability.status}`
    : "None required";
  const collectionText =
    `${collection.kind}: ${collection.status}; ` +
    `last success ${formatTime(collection.last_success_at)}`;
  return (
    <section aria-labelledby="comparison" className="rounded border border-slate-200 bg-white p-4">
      <h2 id="comparison" className="text-lg font-semibold">
        Expected versus actual
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        {`Latest evaluation, ${formatTime(evaluation.evaluated_at)}.`}
      </p>
      <dl className="mt-3 grid gap-3 sm:grid-cols-2">
        <div className="rounded border border-slate-200 p-3">
          <dt className="text-sm font-medium text-slate-700">Expected</dt>
          <dd className="mt-1">
            <Value value={evaluation.expected} />
          </dd>
        </div>
        <div className="rounded border border-slate-200 p-3">
          <dt className="text-sm font-medium text-slate-700">Observed</dt>
          <dd className="mt-1">
            {known ? (
              <Value value={evaluation.observed} />
            ) : (
              <span>{`Unknown — ${evaluation.reason}`}</span>
            )}
          </dd>
        </div>
      </dl>
      <p className="mt-3 text-sm">
        <span className="font-medium">{`Outcome: ${outcomeLabels[evaluation.outcome]}. `}</span>
        {outcomeMeaning[evaluation.outcome]}
      </p>
      {known && <p className="mt-1 text-sm text-slate-700">{evaluation.reason}</p>}
      {evaluation.unknown_reasons.length > 0 && (
        <>
          <h3 className="mt-4 font-medium">Why it could not be decided</h3>
          <ol className="mt-1 list-decimal space-y-1 pl-5 text-sm">
            {evaluation.unknown_reasons.map((code, index) => (
              <li key={code}>
                <code className="text-xs">{code}</code>
                {`: ${reasonHelp[code]}`}
                {index === 0 && evaluation.unknown_reasons.length > 1 && " (explains the result)"}
              </li>
            ))}
          </ol>
        </>
      )}
      <h3 className="mt-4 font-medium">Context</h3>
      <dl className="mt-1 divide-y divide-slate-100 text-sm">
        <Field term="Capability">{capabilityText}</Field>
        <Field term="Collection at evaluation">{collectionText}</Field>
        <Field term="Policy inheritance">
          {evaluation.ancestry === "complete"
            ? "Complete: every group above the project was followed."
            : "Incomplete: a group above the project could not be followed."}
        </Field>
        <Field term="Source">
          <code className="text-xs">{evaluation.source}</code>
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
      <p role="status" className="text-sm text-slate-600">
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
      <dl className="mt-2 divide-y divide-slate-100 text-sm">
        <Field term="Evidence ID">{String(observation.id)}</Field>
        <Field term="Source">
          <code className="text-xs">{observation.source}</code>
        </Field>
        <Field term="Reference">
          <code className="text-xs">{observation.source_ref ?? "None"}</code>
        </Field>
        <Field term="Collected at">{formatTime(observation.collected_at)}</Field>
        <Field term="Collection status">{observation.collection_status}</Field>
        {observation.error && <Field term="Collection error">{observation.error}</Field>}
        <Field term="SHA-256 digest">
          <code className="text-xs break-all">{observation.payload_digest ?? "None (no payload)"}</code>
        </Field>
        <Field term="Integrity">
          {observation.digest_valid
            ? "Verified: the digest matches the stored contents."
            : "Failed: the digest does not match the stored contents."}
        </Field>
      </dl>
      {observation.payload && (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer font-medium">Collected data</summary>
          <pre className="mt-1 overflow-x-auto rounded bg-slate-100 p-2 text-xs">
            {JSON.stringify(observation.payload, null, 2)}
          </pre>
        </details>
      )}
    </>
  );
}

function Evidence({ evaluation }: { evaluation: Evaluation }) {
  const flagged = evaluation.unknown_reasons.includes("evidence_digest_mismatch");
  const decided = evaluation.outcome === "pass" || evaluation.outcome === "fail";
  return (
    <section aria-labelledby="evidence" className="rounded border border-slate-200 bg-white p-4">
      <h2 id="evidence" className="text-lg font-semibold">
        Evidence
      </h2>
      {evaluation.observation_id === null ? (
        <>
          {flagged && (
            <p role="alert" className={`mt-2 ${warningClass}`}>
              {integrityWarning}
            </p>
          )}
          <p role={decided ? "alert" : undefined} className={`mt-2 ${warningClass}`}>
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
  const key = `history:${String(findingId)}`;
  const first = useLoad(key, (signal) => getFindingHistory(findingId, null, signal));
  const paged = usePaged<Evaluation, EvaluationPage>(
    key,
    first,
    (page) => page.next_before_id,
    (before) => getFindingHistory(findingId, before),
  );

  let body: ReactNode;
  if (first.error) body = <ErrorMessage error={first.error} retry={first.retry} />;
  else if (first.loading) {
    body = (
      <p role="status" className="text-sm text-slate-600">
        Loading history…
      </p>
    );
  } else if (paged.items.length === 0) {
    body = <p className="text-sm text-slate-600">No evaluations are recorded for this rule.</p>;
  } else {
    body = (
      <div className="space-y-3">
        <div className="overflow-x-auto">
          <table className="mt-2 w-full text-left text-sm">
            <caption className="sr-only">Evaluation history, newest first</caption>
            <thead className="border-b border-slate-200 bg-slate-50">
              <tr>
                {["Evaluated", "Outcome", "Policy version", "Observed", "Reason"].map((header) => (
                  <th key={header} scope="col" className={`${cell} font-medium`}>
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {paged.items.map((evaluation, index) => (
                <tr key={evaluation.id}>
                  <th scope="row" className={`${cell} font-normal whitespace-nowrap`}>
                    {formatTime(evaluation.evaluated_at)}
                    {index === 0 && <span className="ml-1 text-slate-600">(latest)</span>}
                  </th>
                  <td className={cell}>{outcomeLabels[evaluation.outcome]}</td>
                  <td className={cell}>{`v${String(evaluation.policy.version)}`}</td>
                  <td className={cell}>
                    {observedKnown(evaluation) ? formatValue(evaluation.observed) : "Unknown"}
                  </td>
                  <td className={cell}>{evaluation.reason}</td>
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
            className={secondaryClass}
          >
            {paged.pending ? "Loading…" : "Load older evaluations"}
          </button>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="history" className="rounded border border-slate-200 bg-white p-4">
      <h2 id="history" className="text-lg font-semibold">
        History
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        Every evaluation of this rule for the project, newest first, across policy versions.
      </p>
      {body}
    </section>
  );
}

function MissingFinding({ id }: { id: string }) {
  return (
    <section aria-labelledby="page-title" className="space-y-3">
      <h1 id="page-title" className="text-2xl font-semibold">
        Finding not found
      </h1>
      <p className="text-slate-600">{`There is no finding with ID ${id}.`}</p>
      <p>
        <Link to="/findings" className={linkClass}>
          Back to findings
        </Link>
      </p>
    </section>
  );
}

export function FindingDetailPage() {
  const { findingId = "" } = useParams();
  const id = Number(findingId);
  const valid = Number.isSafeInteger(id) && id > 0 && String(id) === findingId;
  const loaded = useLoad(`finding:${findingId}`, (signal) =>
    valid ? getFinding(id, signal) : Promise.reject(new ApiError(404, "No such finding")),
  );

  if (loaded.error?.status === 404) return <MissingFinding id={findingId} />;
  if (loaded.error) {
    return (
      <section aria-labelledby="page-title" className="space-y-4">
        <h1 id="page-title" className="text-2xl font-semibold">
          Finding
        </h1>
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      </section>
    );
  }
  if (!loaded.data) {
    return (
      <section aria-labelledby="page-title" className="space-y-4">
        <h1 id="page-title" className="text-2xl font-semibold">
          Finding
        </h1>
        <p role="status" className="text-slate-600">
          Loading finding…
        </p>
      </section>
    );
  }
  const finding = loaded.data;
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        <p className="text-sm">
          <Link to="/findings" className={linkClass}>
            Findings
          </Link>
        </p>
        <h1 id="page-title" className="mt-1 text-2xl font-semibold">
          {`${finding.rule_id} on ${finding.project.path}`}
        </h1>
        <p className="mt-1 text-slate-600">{`Finding ${String(finding.id)}`}</p>
      </div>
      <EvaluationNote />
      <Summary finding={finding} />
      <ExpectedVersusActual evaluation={finding.latest_evaluation} />
      <Evidence evaluation={finding.latest_evaluation} />
      <ExceptionPanel finding={finding} />
      <History findingId={finding.id} />
      <ExportLink
        label="Export evidence for this project and rule"
        href={evidenceHref({
          projects: String(finding.project.gitlab_id),
          policy: finding.policy.name,
          rule: finding.rule_id,
        })}
      />
    </section>
  );
}
