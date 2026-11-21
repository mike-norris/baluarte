import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { listControls, type FrameworkId } from "../api/controls";
import {
  decideMapping,
  gapLabels,
  getIssueMappings,
  issueHref,
  listPeriods,
  proposeMapping,
  timingLabels,
  type AuditPeriod,
  type Citable,
  type IssueMapping,
} from "../api/register";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { frameworkLabel, frameworkLabels } from "../controls/format";
import { ErrorMessage } from "../components/ErrorMessage";
import { asApiError, Problems } from "../settings/parts";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";

const MIN_RATIONALE = 20;
const MIN_ITEM_RATIONALE = 10;

function failure(error: unknown): string[] {
  const apiError = asApiError(error);
  return [apiError.message, ...errorReasons(apiError)];
}

function useUser() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  return {
    csrf: user?.csrf_token ?? "",
    canPropose: user?.permissions.includes("register:write") ?? false,
    canDecide: user?.permissions.includes("policy:write") ?? false,
  };
}

const STATE_TONE = {
  proposed: "warning",
  active: "success",
  rejected: "secondary",
  withdrawn: "secondary",
} as const;

const STATE_LABEL = {
  proposed: "Awaiting review",
  active: "In force",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
} as const;

/** One mapping: the control, rationale, cited evidence with its timing, gaps and decisions. */
export function MappingCard({
  mapping,
  onChanged,
  showIssue = false,
}: {
  mapping: IssueMapping;
  onChanged: () => void;
  showIssue?: boolean;
}) {
  const { csrf, canDecide } = useUser();
  const [action, setAction] = useState<"reject" | "withdraw" | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);

  async function decide(decision: "approve" | "reject" | "withdraw") {
    setBusy(true);
    setProblems([]);
    try {
      await decideMapping(mapping.id, decision, text.trim(), csrf);
      setAction(null);
      setText("");
      onChanged();
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setBusy(false);
    }
  }

  const ended = mapping.state === "rejected" || mapping.state === "withdrawn";
  return (
    <article
      aria-label={`${frameworkLabel(mapping.framework)} ${mapping.control_id}`}
      className={`card card-body stack-2${ended ? " opacity-75" : ""}`}
    >
      <div className="d-flex flex-wrap align-items-center gap-2">
        <h3 className="fw-semibold mb-0">
          {frameworkLabel(mapping.framework)} {mapping.control_id}
          {mapping.control_title && (
            <span className="fw-normal text-secondary"> · {mapping.control_title}</span>
          )}
        </h3>
        <Badge tone={STATE_TONE[mapping.state]}>{STATE_LABEL[mapping.state]}</Badge>
        {mapping.self_approved && <Badge tone="secondary">Self-approved</Badge>}
        {mapping.development_decision && <Badge tone="warning">Development decision</Badge>}
      </div>
      {showIssue && (
        <p className="mb-0">
          <Link to={issueHref(mapping.project_id, mapping.iid)}>
            {mapping.project}#{mapping.iid}
          </Link>
          {mapping.issue_title && <span className="text-secondary"> {mapping.issue_title}</span>}
        </p>
      )}
      <p className="mb-0 text-prewrap">{mapping.rationale}</p>
      <ul className="list-unstyled stack-1 mb-0">
        {mapping.evidence.map((item) => (
          <li key={item.key} className="border-start ps-3">
            <div className="d-flex flex-wrap gap-2 align-items-center">
              <span>{item.label}</span>
              {item.occurred_at && (
                <span className="small text-secondary">{formatTime(item.occurred_at)}</span>
              )}
              {item.timing && (
                <Badge tone={item.timing === "in_period" ? "success" : "warning"}>
                  {timingLabels[item.timing]}
                </Badge>
              )}
            </div>
            <div className="small text-secondary text-prewrap">{item.rationale}</div>
          </li>
        ))}
      </ul>
      {mapping.gaps.length > 0 && (
        <Notice tone="warning" title="Gaps">
          <ul className="mb-0">
            {mapping.gaps.map((gap) => (
              <li key={gap.code}>
                <strong>{gapLabels[gap.code] ?? gap.code}.</strong> {gap.message}
              </li>
            ))}
          </ul>
        </Notice>
      )}
      <p className="small text-secondary mb-0">
        Proposed by {mapping.proposed_by.display_name ?? mapping.proposed_by.actor} on{" "}
        {formatTime(mapping.proposed_at)}
        {mapping.decided_by && !mapping.self_approved && (
          <>
            ; {mapping.state === "rejected" ? "rejected" : "approved"} by{" "}
            {mapping.decided_by.display_name ?? mapping.decided_by.actor} on{" "}
            {formatTime(mapping.decided_at)}
            {mapping.decision_note ? `: ${mapping.decision_note}` : ""}
          </>
        )}
        {mapping.withdrawn_at && (
          <>
            ; withdrawn by {mapping.withdrawn_by} on {formatTime(mapping.withdrawn_at)}:{" "}
            {mapping.withdraw_reason}
          </>
        )}
        . Rests on observation {mapping.observation_id}.
      </p>
      <Problems messages={problems} />
      {action === null ? (
        <div className="d-flex flex-wrap gap-2">
          {mapping.can_decide && (
            <>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                disabled={busy}
                onClick={() => void decide("approve")}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => {
                  setAction("reject");
                }}
              >
                Reject
              </button>
            </>
          )}
          {mapping.state === "active" && canDecide && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => {
                setAction("withdraw");
              }}
            >
              Withdraw
            </button>
          )}
        </div>
      ) : (
        <form
          className="stack-2"
          onSubmit={(event) => {
            event.preventDefault();
            void decide(action);
          }}
        >
          <label className="form-label d-block">
            {action === "reject" ? "Why it is rejected" : "Why it is withdrawn"}
            <textarea
              className="form-control"
              rows={2}
              required
              minLength={action === "withdraw" ? MIN_RATIONALE : 1}
              value={text}
              onChange={(event) => {
                setText(event.target.value);
              }}
            />
          </label>
          <div className="d-flex gap-2">
            <button type="submit" className="btn btn-primary btn-sm" disabled={busy}>
              {action === "reject" ? "Reject" : "Withdraw"}
            </button>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => {
                setAction(null);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </article>
  );
}

const FRAMEWORKS = Object.keys(frameworkLabels) as FrameworkId[];

function MapForm({
  projectId,
  iid,
  citable,
  reviewRequired,
  onDone,
}: {
  projectId: number;
  iid: number;
  citable: Citable[];
  reviewRequired: boolean;
  onDone: () => void;
}) {
  const { csrf } = useUser();
  const [framework, setFramework] = useState<FrameworkId>("soc2");
  const [controlId, setControlId] = useState("");
  const [rationale, setRationale] = useState("");
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const controls = useLoad(`register-map-controls:${framework}`, (signal) =>
    listControls(framework, signal),
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setProblems([]);
    try {
      await proposeMapping(
        {
          project_id: projectId,
          iid,
          framework,
          control_id: controlId,
          rationale: rationale.trim(),
          items: Object.entries(chosen).map(([key, why]) => ({ key, rationale: why.trim() })),
        },
        csrf,
      );
      setControlId("");
      setRationale("");
      setChosen({});
      onDone();
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setBusy(false);
    }
  }

  const keys = Object.keys(chosen);
  return (
    <form aria-label="Map to a control" className="card card-body stack-2" onSubmit={submit}>
      <h3 className="fw-semibold mb-0">Map to a control</h3>
      <p className="small text-secondary mb-0">
        Cite the captured items that show the control working, each with why.
        {reviewRequired
          ? " Someone else must approve the mapping before it counts."
          : " Review is off, so the mapping counts at once and is recorded as self-approved."}
      </p>
      <div className="row g-2">
        <label className="form-label col-sm-4">
          Framework
          <select
            className="form-select"
            value={framework}
            onChange={(event) => {
              setFramework(event.target.value as FrameworkId);
              setControlId("");
            }}
          >
            {FRAMEWORKS.map((id) => (
              <option key={id} value={id}>
                {frameworkLabels[id]}
              </option>
            ))}
          </select>
        </label>
        <label className="form-label col-sm-8">
          Control
          <select
            className="form-select"
            required
            value={controlId}
            onChange={(event) => {
              setControlId(event.target.value);
            }}
          >
            <option value="">Choose a control</option>
            {(controls.data?.controls ?? []).map((control) => (
              <option key={control.id} value={control.id}>
                {control.id} · {control.title}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="form-label d-block">
        Rationale
        <textarea
          className="form-control"
          rows={2}
          required
          minLength={MIN_RATIONALE}
          value={rationale}
          onChange={(event) => {
            setRationale(event.target.value);
          }}
        />
      </label>
      <fieldset className="stack-1">
        <legend className="form-label">Evidence cited</legend>
        {citable.map((item) => {
          const checked = item.key in chosen;
          return (
            <div key={item.key}>
              <label className="form-check">
                <input
                  type="checkbox"
                  className="form-check-input"
                  checked={checked}
                  onChange={() => {
                    setChosen((current) => {
                      if (!checked) return { ...current, [item.key]: "" };
                      return Object.fromEntries(
                        Object.entries(current).filter(([key]) => key !== item.key),
                      );
                    });
                  }}
                />
                <span className="form-check-label">
                  {item.label}
                  {item.occurred_at && (
                    <span className="small text-secondary"> · {formatTime(item.occurred_at)}</span>
                  )}
                </span>
              </label>
              {checked && (
                <label className="form-label d-block ms-4">
                  Why this item
                  <textarea
                    className="form-control"
                    rows={1}
                    required
                    minLength={MIN_ITEM_RATIONALE}
                    value={chosen[item.key]}
                    onChange={(event) => {
                      setChosen((current) => ({ ...current, [item.key]: event.target.value }));
                    }}
                  />
                </label>
              )}
            </div>
          );
        })}
      </fieldset>
      <Problems messages={problems} />
      <div>
        <button type="submit" className="btn btn-primary" disabled={busy || keys.length === 0}>
          {reviewRequired ? "Propose mapping" : "Add mapping"}
        </button>
      </div>
    </form>
  );
}

export function PeriodSelect({
  periods,
  value,
  onChange,
}: {
  periods: AuditPeriod[];
  value: number | null;
  onChange: (id: number | null) => void;
}) {
  return (
    <label className="form-label mb-0">
      Audit period
      <select
        className="form-select"
        value={value ?? ""}
        onChange={(event) => {
          onChange(event.target.value ? Number(event.target.value) : null);
        }}
      >
        <option value="">None</option>
        {periods.map((period) => (
          <option key={period.id} value={period.id}>
            {period.name} ({period.starts_on} to {period.ends_on}){period.ended_at ? ", ended" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

/** The issue's control mappings, and a form to add one. */
export function IssueControls({
  projectId,
  iid,
  auditable,
}: {
  projectId: number;
  iid: number;
  auditable: boolean;
}) {
  const { canPropose } = useUser();
  const [periodId, setPeriodId] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const periods = useLoad("register-periods", (signal) => listPeriods(signal));
  const mappings = useLoad(
    `register-mappings:${String(projectId)}:${String(iid)}:${String(periodId)}:${String(version)}`,
    (signal) => getIssueMappings(projectId, iid, periodId, signal),
  );
  const refresh = () => {
    setVersion((current) => current + 1);
  };

  return (
    <section aria-label="Controls" className="stack-2">
      <div className="d-flex flex-wrap align-items-end justify-content-between gap-2">
        <h2 className="fw-semibold mb-0">Controls</h2>
        {periods.data && periods.data.items.length > 0 && (
          <PeriodSelect periods={periods.data.items} value={periodId} onChange={setPeriodId} />
        )}
      </div>
      {mappings.loading && <Loading label="Loading the control mappings…" />}
      {mappings.error && <ErrorMessage error={mappings.error} retry={mappings.retry} />}
      {mappings.data && (
        <>
          {mappings.data.items.length === 0 && (
            <p className="text-secondary mb-0">The issue is not mapped to any control.</p>
          )}
          {mappings.data.items.map((mapping) => (
            <MappingCard key={mapping.id} mapping={mapping} onChanged={refresh} />
          ))}
          {canPropose && auditable && mappings.data.citable.length > 0 && (
            <MapForm
              projectId={projectId}
              iid={iid}
              citable={mappings.data.citable}
              reviewRequired={mappings.data.review_required}
              onDone={refresh}
            />
          )}
        </>
      )}
    </section>
  );
}
