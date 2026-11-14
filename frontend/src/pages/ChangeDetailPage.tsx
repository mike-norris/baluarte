import type { ReactNode } from "react";
import { Link, useParams } from "react-router";

import {
  CHANGES,
  getChange,
  type ChangeDetail,
  type ChangeKind,
  type ChangeVersion,
} from "../api/changes";
import { ApiError } from "../api/client";
import { projectHref } from "../api/gitlab";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import {
  approvalsPart,
  changeName,
  changeOutcomeLabels,
  deploymentPart,
  kindLabels,
  pipelinePart,
} from "../changes/format";
import { PartText } from "../changes/PartText";
import { ErrorMessage } from "../components/ErrorMessage";
import { formatTime } from "../time";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

const changedTerm: Record<ChangeKind, string> = {
  merge_request: "Merged",
  direct_push: "Committed",
  deployment: "Deployed",
};

const card = "card card-body mb-3";

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="row g-2 py-2 border-bottom">
      <dt className="col-md-4 fw-medium text-secondary">{term}</dt>
      <dd className="col-md-8 mb-0">{children}</dd>
    </div>
  );
}

function Approvers({ change }: { change: ChangeDetail }) {
  const approvals = change.approvals;
  if (!approvals || approvals.status !== "known" || approvals.approved_by.length === 0) {
    return null;
  }
  return (
    <ul className="mt-1 ps-3" aria-label="Approved by">
      {approvals.approved_by.map((person) => (
        <li key={person.username}>
          {person.username}
          {person.approved_at && ` on ${formatTime(person.approved_at)}`}
        </li>
      ))}
    </ul>
  );
}

function Versions({ versions }: { versions: ChangeVersion[] }) {
  if (versions.length === 0) {
    return (
      <p role="alert" className="alert alert-danger text-body mt-2 mb-0">
        No evidence record is linked to this change, so it is not backed by collected data.
      </p>
    );
  }
  return (
    <div className="table-responsive mt-2">
      <table className="table table-vcenter mb-0">
        <caption className="visually-hidden">Evidence versions, newest first</caption>
        <thead>
          <tr>
            {["Collected", "Source", "SHA-256 digest", "Integrity", "Contents"].map((header) => (
              <th key={header} scope="col">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {versions.map((version, index) => (
            <tr key={version.observation_id}>
              <th scope="row" className="fw-normal text-nowrap">
                {formatTime(version.collected_at)}
                {index === 0 && <span className="ms-1 text-secondary">(latest)</span>}
                <span className="d-block small text-secondary">
                  {`Evidence ${String(version.observation_id)}`}
                </span>
              </th>
              <td>
                <code>{version.source}</code>
              </td>
              <td>
                <code className="text-break">{version.payload_digest ?? "None (no payload)"}</code>
              </td>
              <td>
                {version.digest_valid ? (
                  "Verified"
                ) : (
                  <span role="alert" className="fw-medium text-danger-emphasis">
                    Failed: the digest does not match the stored contents
                  </span>
                )}
              </td>
              <td>
                {version.payload ? (
                  <details>
                    <summary>Collected data</summary>
                    <pre className="mt-1 mb-0 small" style={{ maxWidth: "36rem" }}>
                      {JSON.stringify(version.payload, null, 2)}
                    </pre>
                  </details>
                ) : (
                  "None"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const back = <Link to={CHANGES}>Changes</Link>;

export function ChangeDetailPage() {
  const { changeId = "" } = useParams();
  const asOf = useAsOf();
  const id = Number(changeId);
  const valid = Number.isSafeInteger(id) && id > 0 && String(id) === changeId;
  const loaded = useLoad(`change:${changeId}@${asOf ?? "now"}`, (signal) =>
    valid ? getChange(id, asOf, signal) : Promise.reject(new ApiError(404, "No such change")),
  );

  if (loaded.error?.status === 404) {
    return (
      <section aria-labelledby="page-title">
        <PageHeader pretitle={back} title="Change not found" />
        <EmptyState title={`There is no change with ID ${changeId}.`} />
      </section>
    );
  }
  if (loaded.error || !loaded.data) {
    return (
      <section aria-labelledby="page-title">
        <PageHeader pretitle={back} title="Change" />
        {loaded.error ? (
          <ErrorMessage error={loaded.error} retry={loaded.retry} />
        ) : (
          <Loading label="Loading change…" />
        )}
      </section>
    );
  }
  const change = loaded.data;
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={back}
        title={changeName(change)}
        subtitle={`${kindLabels[change.kind]} on ${change.project.path}`}
      />
      <AsOfResult answer={change} />
      {change.complete === false && (
        <p className="alert alert-warning text-body">
          Part of this change could not be read last time. It will be read again; until then the
          missing parts are unknown.
        </p>
      )}
      <section aria-labelledby="summary" className={card}>
        <h2 id="summary" className="card-title">
          Summary
        </h2>
        <dl className="mb-0">
          <Field term="Status">
            {changeOutcomeLabels[change.outcome] ?? change.outcome}
            {change.reasons.map((reason) => (
              <span key={reason} className="d-block text-secondary">
                {reason}
              </span>
            ))}
          </Field>
          <Field term="Project">
            <Link to={projectHref(change.project.gitlab_id)}>{change.project.path}</Link>
          </Field>
          <Field term="Commit">
            <code className="text-break">{change.sha}</code>
          </Field>
          {change.web_path && (
            <Field term="In GitLab">
              <code>{change.web_path}</code>
            </Field>
          )}
          <Field term="Author">{change.author ?? "Unknown"}</Field>
          {change.kind === "merge_request" && (
            <Field term="Merged by">{change.merged_by ?? "Unknown"}</Field>
          )}
          <Field term={changedTerm[change.kind]}>{formatTime(change.changed_at)}</Field>
          <Field term="Approvals">
            <PartText part={approvalsPart(change.approvals)} />
            <Approvers change={change} />
          </Field>
          <Field term="Pipeline and tests">
            <PartText part={pipelinePart(change.pipeline)} />
          </Field>
          <Field term="Deployment">
            <PartText part={deploymentPart(change.deployments)} />
          </Field>
        </dl>
      </section>
      <section aria-labelledby="versions" className={card}>
        <h2 id="versions" className="card-title">
          Evidence
        </h2>
        <p className="mt-1 text-secondary">
          Every read of this change is kept. A later read, for example once a deployment appeared,
          adds a version; earlier ones are never changed.
        </p>
        <Versions versions={change.versions} />
      </section>
    </section>
  );
}
