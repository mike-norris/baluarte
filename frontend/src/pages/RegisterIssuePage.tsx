import { Link, useParams } from "react-router";

import {
  categoryLabels,
  fileHref,
  getIssue,
  getIssueEvidence,
  type Capture,
  type CapturedNote,
  type HistoryEvent,
  type IssueEvidence,
} from "../api/register";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { DigestDisplay } from "../ui/DigestDisplay";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { PageHeader } from "../ui/PageHeader";

import { IssueControls } from "./RegisterMappings";
import { ExportButton } from "./RegisterExport";

const SOURCE_LABELS: Record<string, string> = {
  issue: "Issue",
  notes: "Comments",
  state_events: "State history",
  label_events: "Label history",
  milestone_events: "Milestone history",
  links: "Linked issues",
  closed_by: "Closing merge requests",
  related_merge_requests: "Related merge requests",
  merge_requests: "Merge requests",
  approvals: "Approvals",
  pipelines: "Pipelines",
  releases: "Releases",
  attachments: "Attachments",
  timeline: "Timeline",
  pull_requests: "Pull requests",
};

/** What a provider calls a merge request, how it numbers one, and what it calls a pipeline. */
function changeWords(capture: Capture) {
  return capture.provider === "github"
    ? { plural: "Pull requests", one: "Pull request", mark: "#", run: "Latest workflow run" }
    : { plural: "Merge requests", one: "Merge request", mark: "!", run: "Latest pipeline" };
}

function bytes(size: number | null): string {
  if (size === null) return "";
  if (size < 1024) return `${String(size)} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function eventText(event: HistoryEvent): string {
  if (event.kind === "state") {
    return `${event.state ?? "changed"} the issue${event.merge_request ? ` (${event.merge_request})` : ""}`;
  }
  if (event.kind === "label") {
    return `${event.action === "remove" ? "removed" : "added"} label ${event.label ?? ""}`;
  }
  return `${event.action === "remove" ? "removed" : "set"} milestone ${event.milestone ?? ""}`;
}

type Entry =
  | { at: string; kind: "note"; note: CapturedNote }
  | { at: string; kind: "event"; event: HistoryEvent };

function timeline(capture: Capture): Entry[] {
  const entries: Entry[] = [
    ...(capture.notes ?? []).map((note): Entry => ({
      at: note.created_at ?? "",
      kind: "note",
      note,
    })),
    ...(capture.history ?? []).map((event): Entry => ({
      at: event.created_at ?? "",
      kind: "event",
      event,
    })),
  ];
  return entries.sort((a, b) => a.at.localeCompare(b.at));
}

function Timeline({ capture }: { capture: Capture }) {
  const entries = timeline(capture);
  if (entries.length === 0) return <p className="text-secondary mb-0">No comments or history.</p>;
  return (
    <ul className="list-unstyled stack-2 mb-0">
      {entries.map((entry, index) => (
        <li key={index} className="border-start ps-3">
          <div className="small text-secondary">
            {formatTime(entry.at || null)}
            {" · "}
            {entry.kind === "note"
              ? (entry.note.author ?? "unknown")
              : (entry.event.user ?? "unknown")}
            {entry.kind === "note" && entry.note.internal && (
              <>
                {" "}
                <Badge tone="warning">Internal</Badge>
              </>
            )}
          </div>
          {entry.kind === "note" ? (
            <div className={entry.note.system ? "text-secondary" : "text-prewrap"}>
              {entry.note.body}
            </div>
          ) : (
            <div className="text-secondary">{eventText(entry.event)}</div>
          )}
        </li>
      ))}
    </ul>
  );
}

function CaptureStatus({ evidence }: { evidence: IssueEvidence }) {
  return (
    <section aria-label="Capture" className="card card-body stack-2">
      <div className="d-flex flex-wrap align-items-center gap-2">
        <h2 className="fw-semibold mb-0">Captured evidence</h2>
        <Badge
          tone={
            evidence.status === "complete"
              ? "success"
              : evidence.status === "partial"
                ? "warning"
                : "danger"
          }
        >
          {evidence.status === "complete"
            ? "Complete"
            : evidence.status === "partial"
              ? "Partial"
              : "Failed"}
        </Badge>
        {evidence.digest_verified === false && <Badge tone="danger">Digest mismatch</Badge>}
      </div>
      <p className="mb-0 text-secondary">
        Captured {formatTime(evidence.captured_at)}, reflecting GitLab&apos;s change of{" "}
        {formatTime(evidence.captured_for)}. Observation {evidence.observation_id}.
      </p>
      {evidence.payload_digest && (
        <DigestDisplay value={evidence.payload_digest} label="SHA-256 of the capture" />
      )}
      {evidence.gaps.length > 0 && (
        <Notice tone="warning" title="Not captured">
          <ul className="mb-0">
            {evidence.gaps.map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
        </Notice>
      )}
    </section>
  );
}

function Sources({ capture }: { capture: Capture }) {
  return (
    <ul className="list-inline mb-0">
      {Object.entries(capture.sources).map(([source, state]) => (
        <li key={source} className="list-inline-item">
          <Badge
            tone={state === "ok" ? "success" : state === "not_applicable" ? "secondary" : "warning"}
          >
            {`${SOURCE_LABELS[source] ?? source}: ${state.replace("_", " ")}`}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

function EvidenceSections({ capture }: { capture: Capture }) {
  const mergeRequests = capture.merge_requests ?? [];
  const words = changeWords(capture);
  const attachments = capture.attachments ?? [];
  return (
    <>
      <section aria-label="Sources" className="card card-body stack-2">
        <h2 className="fw-semibold mb-0">Sources read</h2>
        <Sources capture={capture} />
      </section>
      <section aria-label="Description" className="card card-body stack-2">
        <h2 className="fw-semibold mb-0">Description</h2>
        {capture.issue?.description ? (
          <div className="text-prewrap">{capture.issue.description}</div>
        ) : (
          <p className="text-secondary mb-0">No description.</p>
        )}
        {capture.issue?.closed_by && (
          <p className="mb-0 text-secondary">Closed by {capture.issue.closed_by}.</p>
        )}
      </section>
      <section aria-label="Comments and history" className="card card-body stack-2">
        <h2 className="fw-semibold mb-0">Comments and history</h2>
        <Timeline capture={capture} />
      </section>
      <section aria-label={words.plural} className="card">
        <div className="card-header">
          <h2 className="card-title">{words.plural}</h2>
        </div>
        {mergeRequests.length === 0 ? (
          <div className="card-body text-secondary">No linked {words.plural.toLowerCase()}.</div>
        ) : (
          <div className="table-responsive">
            <table className="table table-vcenter card-table">
              <thead>
                <tr>
                  <th>{words.one}</th>
                  <th>Relation</th>
                  <th>State</th>
                  <th>Approved by</th>
                  <th>{words.run}</th>
                </tr>
              </thead>
              <tbody>
                {mergeRequests.map((mr) => (
                  <tr key={`${String(mr.project_id)}-${String(mr.iid)}`}>
                    <td>
                      {mr.web_url ? (
                        <a href={mr.web_url} target="_blank" rel="noreferrer">
                          {words.mark}
                          {mr.iid} {mr.title}
                        </a>
                      ) : (
                        `${words.mark}${String(mr.iid)} ${mr.title ?? ""}`
                      )}
                    </td>
                    <td>{mr.relation === "closes" ? "Closes the issue" : "Related"}</td>
                    <td>
                      {mr.state}
                      {mr.merged_at && (
                        <div className="small text-secondary">
                          {formatTime(mr.merged_at)}
                          {mr.merged_by ? ` by ${mr.merged_by}` : ""}
                        </div>
                      )}
                    </td>
                    <td>
                      {mr.approvals === null
                        ? "Not read"
                        : mr.approvals.approved_by.join(", ") || "Nobody"}
                    </td>
                    <td>
                      {mr.pipelines === null ? "Not read" : (mr.pipelines[0]?.status ?? "None")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section aria-label="Linked issues and releases" className="card card-body stack-2">
        <h2 className="fw-semibold mb-0">Linked issues and releases</h2>
        {(capture.links ?? []).length === 0 && (capture.releases ?? []).length === 0 ? (
          <p className="text-secondary mb-0">None.</p>
        ) : (
          <ul className="mb-0">
            {(capture.links ?? []).map((link) => (
              <li key={`link-${String(link.project_id)}-${String(link.iid)}`}>
                {link.link_type?.replace("_", " ") ?? "linked"}: #{link.iid} {link.title} (
                {link.state})
              </li>
            ))}
            {(capture.releases ?? []).map((release) => (
              <li key={`release-${release.tag_name ?? ""}`}>
                Release {release.tag_name}
                {release.released_at ? `, ${formatTime(release.released_at)}` : ""}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-label="Attachments" className="card">
        <div className="card-header">
          <h2 className="card-title">Attachments</h2>
        </div>
        {attachments.length === 0 ? (
          <div className="card-body text-secondary">No attachments.</div>
        ) : (
          <div className="table-responsive">
            <table className="table table-vcenter card-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Size</th>
                  <th>SHA-256</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {attachments.map((file) => (
                  <tr key={`${file.secret}-${file.filename}`}>
                    <td>
                      {file.sha256 && file.status === "ok" ? (
                        <a href={fileHref(file.sha256)} download={file.filename}>
                          {file.filename}
                        </a>
                      ) : (
                        file.filename
                      )}
                      <div className="small text-secondary">In {file.referenced_in.join(", ")}</div>
                    </td>
                    <td>{bytes(file.size_bytes)}</td>
                    <td>{file.sha256 ? <DigestDisplay value={file.sha256} label="" /> : "—"}</td>
                    <td>{file.status === "ok" ? "Captured" : (file.error ?? file.status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

export function RegisterIssuePage() {
  const params = useParams();
  const projectId = Number(params["projectId"]);
  const iid = Number(params["iid"]);
  const detail = useLoad(`register-issue:${String(projectId)}:${String(iid)}`, (signal) =>
    getIssue(projectId, iid, signal),
  );
  const evidence = useLoad(`register-evidence:${String(projectId)}:${String(iid)}`, (signal) =>
    getIssueEvidence(projectId, iid, signal),
  );
  const issue = detail.data?.issue ?? null;
  const name = detail.data ? `${detail.data.project}#${String(iid)}` : `#${String(iid)}`;

  return (
    <div className="stack-3">
      <PageHeader
        pretitle="Issue register"
        title={name}
        subtitle={issue?.confidential ? "Confidential issue" : (issue?.title ?? undefined)}
      />
      <p className="mb-0">
        <Link to="/register">Back to the register</Link>
        {issue?.web_url && (
          <>
            {" · "}
            <a href={issue.web_url} target="_blank" rel="noreferrer">
              Open in GitLab
            </a>
          </>
        )}
      </p>
      {detail.loading && <Loading label="Loading the issue…" />}
      {detail.error && <ErrorMessage error={detail.error} retry={detail.retry} />}
      {detail.data && (
        <section aria-label="Register" className="card card-body stack-2">
          <div className="d-flex flex-wrap gap-2 align-items-center">
            <Badge tone={detail.data.auditable ? "success" : "secondary"}>
              {detail.data.auditable ? "Auditable" : "Not in the register"}
            </Badge>
            {issue && <Badge tone="info">{categoryLabels[issue.category]}</Badge>}
            {issue?.state && <span>{issue.state === "closed" ? "Closed" : "Open"}</span>}
          </div>
          {issue && issue.why.length > 0 && (
            <ul className="mb-0">
              {issue.why.map((reason, index) => (
                <li key={index}>
                  {reason.kind === "rule"
                    ? `Rule: ${reason.rule_name ?? ""}`
                    : `${reason.kind === "included" ? "Included" : "Excluded"} by ${reason.decided_by ?? ""}: ${reason.reason ?? ""}`}
                </li>
              ))}
            </ul>
          )}
          {detail.data.decisions.length > 0 && (
            <details>
              <summary>Every decision on this issue ({detail.data.decisions.length})</summary>
              <ul className="mb-0">
                {detail.data.decisions.map((decision) => (
                  <li key={decision.id}>
                    {decision.decision === "include" ? "Included" : "Excluded"} by{" "}
                    {decision.decided_by} on {formatTime(decision.decided_at)}: {decision.reason}
                    {decision.ended_at &&
                      ` (ended by ${decision.ended_by ?? ""} on ${formatTime(decision.ended_at)})`}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <p className="small text-secondary mb-0">
            Closing an issue is never taken as proof that a control works.
          </p>
          {detail.data.auditable && (
            <ExportButton
              label="Export this issue"
              request={{ issues: [{ project_id: projectId, iid }] }}
            />
          )}
        </section>
      )}
      {evidence.loading && <Loading label="Loading the captured evidence…" />}
      {evidence.error && <ErrorMessage error={evidence.error} retry={evidence.retry} />}
      {evidence.data && !evidence.data.captured && (
        <EmptyState title="Not captured yet">
          The evidence of auditable issues is captured after their issues are read; this one has not
          been captured yet.
        </EmptyState>
      )}
      {detail.data && evidence.data?.captured && (
        <IssueControls projectId={projectId} iid={iid} auditable={detail.data.auditable} />
      )}
      {evidence.data?.captured && (
        <>
          <CaptureStatus evidence={evidence.data} />
          {evidence.data.capture && <EvidenceSections capture={evidence.data.capture} />}
        </>
      )}
    </div>
  );
}
