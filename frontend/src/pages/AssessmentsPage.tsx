import { useState, type FormEvent } from "react";

import {
  assessmentAction,
  assessmentStateLabels,
  citationKindLabels,
  citeEvidence,
  conclusionLabels,
  deleteAssessment,
  getAssessment,
  listAssessments,
  removeCitation,
  startAssessment,
  type Assessment,
  type AssessmentState,
  type CitationKind,
  type Conclusion,
} from "../api/assessments";
import { frameworkIds } from "../api/controls";
import { conclusionTones } from "../assessments/format";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { frameworkLabel } from "../controls/format";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { PageHeader } from "../ui/PageHeader";

const stateTones: Record<AssessmentState, Tone> = {
  draft: "secondary",
  submitted: "info",
  approved: "success",
  rejected: "danger",
};

const NARRATIVE_MIN = 20;

function usePermissions() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const permissions = user?.permissions ?? [];
  return {
    csrf: user?.csrf_token ?? "",
    userId: user?.user_id ?? null,
    canWrite: permissions.includes("assessments:write"),
    canReview: permissions.includes("assessments:review"),
    development: user?.development_mode === true,
  };
}

function message(failure: unknown): string {
  return failure instanceof Error ? failure.message : String(failure);
}

function person(ref: { display_name: string | null; user_id: number }): string {
  return ref.display_name ?? `user ${String(ref.user_id)}`;
}

function controlText(a: Assessment): string {
  return `${frameworkLabel(a.control.framework)} ${a.control.control_id} (${a.control.version})`;
}

/** Start an assessment of a control as a draft. */
function StartForm({ onStarted }: { onStarted: (a: Assessment) => void }) {
  const { csrf } = usePermissions();
  const [framework, setFramework] = useState<string>("soc2");
  const [controlId, setControlId] = useState("");
  const [periodStart, setPeriodStart] = useState("");
  const [periodEnd, setPeriodEnd] = useState("");
  const [conclusion, setConclusion] = useState<Conclusion>("effective");
  const [narrative, setNarrative] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const started = await startAssessment(
        {
          framework,
          control_id: controlId.trim(),
          period_start: periodStart,
          period_end: periodEnd,
          conclusion,
          narrative: narrative.trim(),
        },
        csrf,
      );
      setControlId("");
      setNarrative("");
      onStarted(started);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      aria-label="Assess a control"
      className="card card-body mb-3 stack-2"
      onSubmit={(event) => void submit(event)}
    >
      <h2 className="card-title">Assess a control</h2>
      <div className="d-flex flex-wrap gap-2">
        <label className="form-label mb-0">
          Framework
          <select
            className="form-select mt-1"
            value={framework}
            onChange={(event) => {
              setFramework(event.target.value);
            }}
          >
            {frameworkIds.map((id) => (
              <option key={id} value={id}>
                {frameworkLabel(id)}
              </option>
            ))}
          </select>
        </label>
        <label className="form-label mb-0">
          Control
          <input
            className="form-control mt-1"
            value={controlId}
            maxLength={20}
            placeholder="CC8.1"
            onChange={(event) => {
              setControlId(event.target.value);
            }}
          />
        </label>
        <label className="form-label mb-0">
          Period from
          <input
            className="form-control mt-1"
            type="date"
            value={periodStart}
            onChange={(event) => {
              setPeriodStart(event.target.value);
            }}
          />
        </label>
        <label className="form-label mb-0">
          Period to
          <input
            className="form-control mt-1"
            type="date"
            value={periodEnd}
            onChange={(event) => {
              setPeriodEnd(event.target.value);
            }}
          />
        </label>
        <label className="form-label mb-0">
          Conclusion
          <select
            className="form-select mt-1"
            value={conclusion}
            onChange={(event) => {
              setConclusion(event.target.value as Conclusion);
            }}
          >
            {(Object.keys(conclusionLabels) as Conclusion[]).map((value) => (
              <option key={value} value={value}>
                {conclusionLabels[value]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="form-label">
        What was tested and how
        <textarea
          className="form-control mt-1"
          rows={3}
          maxLength={8000}
          value={narrative}
          onChange={(event) => {
            setNarrative(event.target.value);
          }}
        />
      </label>
      {error && <p className="text-danger mb-0">{error}</p>}
      <div>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={
            busy ||
            !controlId.trim() ||
            !periodStart ||
            !periodEnd ||
            narrative.trim().length < NARRATIVE_MIN
          }
        >
          Start as draft
        </button>
      </div>
    </form>
  );
}

/** Cite one piece of evidence in a draft. */
function CiteForm({
  assessment,
  onCited,
}: {
  assessment: Assessment;
  onCited: (a: Assessment) => void;
}) {
  const { csrf } = usePermissions();
  const [kind, setKind] = useState<CitationKind>("evidence_item");
  const [target, setTarget] = useState("");
  const [rationale, setRationale] = useState("");
  const [error, setError] = useState<string | null>(null);
  const needsTarget = kind !== "control_status";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const updated = await citeEvidence(
        assessment.id,
        { kind, target_id: needsTarget ? Number(target) : null, rationale: rationale.trim() },
        csrf,
      );
      setTarget("");
      setRationale("");
      onCited(updated);
    } catch (failure) {
      setError(message(failure));
    }
  }

  return (
    <form
      aria-label="Cite evidence"
      className="border rounded p-2 mb-2 stack-2"
      onSubmit={(event) => void submit(event)}
    >
      <div className="d-flex flex-wrap gap-2">
        <label className="form-label mb-0">
          Evidence
          <select
            className="form-select mt-1"
            value={kind}
            onChange={(event) => {
              setKind(event.target.value as CitationKind);
            }}
          >
            {(Object.keys(citationKindLabels) as CitationKind[]).map((value) => (
              <option key={value} value={value}>
                {citationKindLabels[value]}
              </option>
            ))}
          </select>
        </label>
        {needsTarget && (
          <label className="form-label mb-0">
            ID
            <input
              className="form-control mt-1"
              inputMode="numeric"
              value={target}
              onChange={(event) => {
                setTarget(event.target.value.replace(/\D/g, ""));
              }}
            />
          </label>
        )}
      </div>
      <label className="form-label">
        Why it supports the conclusion
        <input
          className="form-control mt-1"
          maxLength={2000}
          value={rationale}
          onChange={(event) => {
            setRationale(event.target.value);
          }}
        />
      </label>
      {error && <p className="text-danger mb-0">{error}</p>}
      <div>
        <button
          type="submit"
          className="btn"
          disabled={!rationale.trim() || (needsTarget && !target)}
        >
          Cite
        </button>
      </div>
    </form>
  );
}

/** One assessment: the conclusion, what it cites, its review, and what the viewer may do. */
function AssessmentDetail({
  assessment,
  onChanged,
}: {
  assessment: Assessment;
  onChanged: (text: string, a: Assessment | null) => void;
}) {
  const { csrf, userId, canWrite, canReview, development } = usePermissions();
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const own = userId === assessment.assessor.user_id;
  const draft = assessment.state === "draft";
  const editable = canWrite && own && draft;
  const heading = `Assessment of ${controlText(assessment)}`;

  async function run(task: () => Promise<Assessment | null>, text: string) {
    setError(null);
    try {
      onChanged(text, await task());
    } catch (failure) {
      setError(message(failure));
    }
  }

  return (
    <section aria-label={heading} className="card card-body mb-3">
      <h2 className="card-title">{heading}</h2>
      <div className="d-flex flex-wrap gap-2 mb-2">
        <Badge tone={stateTones[assessment.state]}>{assessmentStateLabels[assessment.state]}</Badge>
        <Badge tone={conclusionTones[assessment.conclusion]}>
          {conclusionLabels[assessment.conclusion]}
        </Badge>
      </div>
      <p className="text-secondary">{assessment.control.title}</p>
      <p>{assessment.narrative}</p>
      <dl className="row mb-2">
        <dt className="col-sm-3">Period</dt>
        <dd className="col-sm-9">{`${assessment.period_start} to ${assessment.period_end}`}</dd>
        <dt className="col-sm-3">Assessor</dt>
        <dd className="col-sm-9">{`${person(assessment.assessor)}, ${formatTime(assessment.created_at)}`}</dd>
        {assessment.reviewed_by && (
          <>
            <dt className="col-sm-3">Reviewed by</dt>
            <dd className="col-sm-9">
              {`${person(assessment.reviewed_by)}, ${formatTime(assessment.reviewed_at)}${assessment.review_note ? `: “${assessment.review_note}”` : ""}${assessment.development_review ? " (their own, in development mode)" : ""}`}
            </dd>
          </>
        )}
        {assessment.supersedes_id !== null && (
          <>
            <dt className="col-sm-3">Supersedes</dt>
            <dd className="col-sm-9">{`Assessment ${String(assessment.supersedes_id)}`}</dd>
          </>
        )}
        {assessment.superseded_by_id !== null && (
          <>
            <dt className="col-sm-3">Superseded by</dt>
            <dd className="col-sm-9">{`Assessment ${String(assessment.superseded_by_id)}`}</dd>
          </>
        )}
      </dl>
      <h3 className="h4">Evidence cited</h3>
      {assessment.citations.length === 0 ? (
        <p className="text-secondary">Nothing cited yet.</p>
      ) : (
        <ul aria-label="Citations" className="list-unstyled">
          {assessment.citations.map((c) => (
            <li key={c.id} className="mb-2">
              <strong>{`${citationKindLabels[c.kind]}: `}</strong>
              {c.title}
              <div>{c.rationale}</div>
              {c.digest && (
                <div className="text-secondary small text-break">{`SHA-256 ${c.digest}`}</div>
              )}
              {editable && (
                <button
                  type="button"
                  className="btn btn-sm btn-danger mt-1"
                  onClick={() =>
                    void run(
                      () => removeCitation(assessment.id, c.id, csrf),
                      "Removed the citation.",
                    )
                  }
                >
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && (
        <CiteForm
          assessment={assessment}
          onCited={(a) => {
            onChanged("Cited the evidence.", a);
          }}
        />
      )}
      {error && (
        <p role="alert" className="text-danger">
          {error}
        </p>
      )}
      {editable && (
        <div className="btn-list">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() =>
              void run(
                () => assessmentAction(assessment.id, "submit", csrf),
                "Submitted for review.",
              )
            }
          >
            Submit for review
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() =>
              void run(async () => {
                await deleteAssessment(assessment.id, csrf);
                return null;
              }, "Deleted the draft.")
            }
          >
            Delete draft
          </button>
        </div>
      )}
      {canReview && assessment.state === "submitted" && (!own || development) && (
        <div className="mt-3">
          <label className="form-label" htmlFor={`note-${String(assessment.id)}`}>
            Review note
          </label>
          <textarea
            id={`note-${String(assessment.id)}`}
            className="form-control mb-2"
            rows={2}
            maxLength={2000}
            value={note}
            onChange={(event) => {
              setNote(event.target.value);
            }}
          />
          <div className="btn-list">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() =>
                void run(() => assessmentAction(assessment.id, "approve", csrf, note), "Approved.")
              }
            >
              Approve
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={!note.trim()}
              onClick={() =>
                void run(() => assessmentAction(assessment.id, "reject", csrf, note), "Rejected.")
              }
            >
              Reject
            </button>
          </div>
        </div>
      )}
      {canReview && assessment.state === "submitted" && own && !development && (
        <Notice tone="info" title="Someone else reviews it">
          You made this assessment, so another reviewer approves or rejects it.
        </Notice>
      )}
    </section>
  );
}

/** People's conclusions about controls, each citing evidence and approved by someone other than
 * the assessor; kept beside the automated status, never merged with it. */
export function AssessmentsPage() {
  const { canWrite } = usePermissions();
  const [state, setState] = useState<AssessmentState | "">("");
  const [reload, setReload] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const loaded = useLoad(`assessments:${state}#${String(reload)}`, (signal) =>
    listAssessments({ state: state || null }, signal),
  );
  const open = useLoad(`assessment:${String(openId)}#${String(reload)}`, (signal) =>
    openId === null ? Promise.resolve(null) : getAssessment(openId, signal),
  );

  function changed(text: string, a: Assessment | null) {
    setStatus(text);
    setOpenId(a?.id ?? null);
    setReload((value) => value + 1);
  }

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Assessments"
        subtitle="People's conclusions about controls, citing evidence and approved by someone other than the assessor."
      />
      {status && (
        <p role="status" className="alert alert-success text-body">
          {status}
        </p>
      )}
      {canWrite && (
        <StartForm
          onStarted={(a) => {
            changed(`Started an assessment of ${a.control.control_id}.`, a);
          }}
        />
      )}
      {open.data && (
        <AssessmentDetail
          key={`${String(open.data.id)}:${open.data.state}:${String(open.data.citations.length)}`}
          assessment={open.data}
          onChanged={changed}
        />
      )}
      <label className="form-label">
        State
        <select
          className="form-select mt-1"
          value={state}
          onChange={(event) => {
            setState(event.target.value as AssessmentState | "");
          }}
        >
          <option value="">All</option>
          {(Object.keys(assessmentStateLabels) as AssessmentState[]).map((value) => (
            <option key={value} value={value}>
              {assessmentStateLabels[value]}
            </option>
          ))}
        </select>
      </label>
      {loaded.error ? (
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      ) : !loaded.data ? (
        <Loading label="Loading assessments…" />
      ) : loaded.data.items.length === 0 ? (
        <EmptyState title="No assessments yet">
          Assess a control for a period, cite the evidence, then submit it for review.
        </EmptyState>
      ) : (
        <div className="table-responsive">
          <table className="table card-table" aria-label="Assessments">
            <thead>
              <tr>
                <th>Control</th>
                <th>Period</th>
                <th>Conclusion</th>
                <th>State</th>
                <th>Assessor</th>
              </tr>
            </thead>
            <tbody>
              {loaded.data.items.map((a) => (
                <tr key={a.id}>
                  <td>
                    <button
                      type="button"
                      className="btn btn-link p-0"
                      aria-label={`Open assessment ${String(a.id)}`}
                      onClick={() => {
                        setOpenId(a.id);
                      }}
                    >
                      {controlText(a)}
                    </button>
                  </td>
                  <td>{`${a.period_start} to ${a.period_end}`}</td>
                  <td>
                    <Badge tone={conclusionTones[a.conclusion]}>
                      {conclusionLabels[a.conclusion]}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={stateTones[a.state]}>{assessmentStateLabels[a.state]}</Badge>
                  </td>
                  <td>{person(a.assessor)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
