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
import { linkClass } from "../components/styles";
import { formatTime } from "../time";

const changedTerm: Record<ChangeKind, string> = {
  merge_request: "Merged",
  direct_push: "Committed",
  deployment: "Deployed",
};

const card = "rounded border border-slate-200 bg-white p-4";
const cell = "px-3 py-2 align-top";

function Field({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="py-2 sm:grid sm:grid-cols-3 sm:gap-4">
      <dt className="font-medium text-slate-700">{term}</dt>
      <dd className="mt-1 sm:col-span-2 sm:mt-0">{children}</dd>
    </div>
  );
}

function Approvers({ change }: { change: ChangeDetail }) {
  const approvals = change.approvals;
  if (!approvals || approvals.status !== "known" || approvals.approved_by.length === 0) {
    return null;
  }
  return (
    <ul className="mt-1 list-disc pl-5" aria-label="Approved by">
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
      <p role="alert" className="mt-2 rounded border border-red-300 bg-red-50 p-3 text-sm">
        No evidence record is linked to this change, so it is not backed by collected data.
      </p>
    );
  }
  return (
    <div className="mt-2 overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Evidence versions, newest first</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            {["Collected", "Source", "SHA-256 digest", "Integrity", "Contents"].map((header) => (
              <th key={header} scope="col" className={`${cell} font-medium`}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {versions.map((version, index) => (
            <tr key={version.observation_id}>
              <th scope="row" className={`${cell} font-normal whitespace-nowrap`}>
                {formatTime(version.collected_at)}
                {index === 0 && <span className="ml-1 text-slate-600">(latest)</span>}
                <span className="block text-xs text-slate-600">
                  {`Evidence ${String(version.observation_id)}`}
                </span>
              </th>
              <td className={cell}>
                <code className="text-xs">{version.source}</code>
              </td>
              <td className={cell}>
                <code className="text-xs break-all">
                  {version.payload_digest ?? "None (no payload)"}
                </code>
              </td>
              <td className={cell}>
                {version.digest_valid ? (
                  "Verified"
                ) : (
                  <span role="alert" className="font-medium text-red-800">
                    Failed: the digest does not match the stored contents
                  </span>
                )}
              </td>
              <td className={cell}>
                {version.payload ? (
                  <details>
                    <summary className="cursor-pointer">Collected data</summary>
                    <pre className="mt-1 max-w-xl overflow-x-auto rounded bg-slate-100 p-2 text-xs">
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

function Title({ children }: { children: ReactNode }) {
  return (
    <h1 id="page-title" className="mt-1 text-2xl font-semibold">
      {children}
    </h1>
  );
}

const back = (
  <p className="text-sm">
    <Link to={CHANGES} className={linkClass}>
      Changes
    </Link>
  </p>
);

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
      <section aria-labelledby="page-title" className="space-y-3">
        {back}
        <Title>Change not found</Title>
        <p className="text-slate-600">{`There is no change with ID ${changeId}.`}</p>
      </section>
    );
  }
  if (loaded.error || !loaded.data) {
    return (
      <section aria-labelledby="page-title" className="space-y-4">
        {back}
        <Title>Change</Title>
        {loaded.error ? (
          <ErrorMessage error={loaded.error} retry={loaded.retry} />
        ) : (
          <p role="status" className="text-slate-600">
            Loading change…
          </p>
        )}
      </section>
    );
  }
  const change = loaded.data;
  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <div>
        {back}
        <Title>{changeName(change)}</Title>
        <p className="mt-1 text-slate-600">{`${kindLabels[change.kind]} on ${change.project.path}`}</p>
      </div>
      <AsOfResult answer={change} />
      {change.complete === false && (
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
          Part of this change could not be read last time. It will be read again; until then the
          missing parts are unknown.
        </p>
      )}
      <section aria-labelledby="summary" className={card}>
        <h2 id="summary" className="text-lg font-semibold">
          Summary
        </h2>
        <dl className="mt-2 divide-y divide-slate-100 text-sm">
          <Field term="Status">
            {changeOutcomeLabels[change.outcome] ?? change.outcome}
            {change.reasons.map((reason) => (
              <span key={reason} className="block text-slate-700">
                {reason}
              </span>
            ))}
          </Field>
          <Field term="Project">
            <Link to={projectHref(change.project.gitlab_id)} className={linkClass}>
              {change.project.path}
            </Link>
          </Field>
          <Field term="Commit">
            <code className="text-xs break-all">{change.sha}</code>
          </Field>
          {change.web_path && (
            <Field term="In GitLab">
              <code className="text-xs">{change.web_path}</code>
            </Field>
          )}
          <Field term="Author">{change.author ?? "Unknown"}</Field>
          {change.kind === "merge_request" && (
            <Field term="Merged by">{change.merged_by ?? "Unknown"}</Field>
          )}
          <Field term={changedTerm[change.kind]}>
            {formatTime(change.changed_at)}
          </Field>
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
        <h2 id="versions" className="text-lg font-semibold">
          Evidence
        </h2>
        <p className="mt-1 text-sm text-slate-600">
          Every read of this change is kept. A later read, for example once a deployment appeared,
          adds a version; earlier ones are never changed.
        </p>
        <Versions versions={change.versions} />
      </section>
    </section>
  );
}
