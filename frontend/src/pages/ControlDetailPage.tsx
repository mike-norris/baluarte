import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router";

import {
  frameworkIds,
  getControl,
  policyName,
  type ControlDetail,
  type FrameworkId,
  type MappingChange,
} from "../api/controls";
import { findingsHref } from "../api/evaluations";
import { useLoad } from "../api/useLoad";
import { useAsOf } from "../asof/context";
import { AsOfResult } from "../asof/AsOfResult";
import { PastReadOnly } from "../asof/PastReadOnly";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { ChangeForm, type ChangeIntent } from "../controls/ChangeForm";
import {
  changePerson,
  changeStateLabels,
  CONTROLS,
  frameworkLabel,
  kindLabels,
  mappingChangeText,
  mappingDecisionText,
  ruleTypeLabel,
  ruleTypes,
  statusMeaning,
} from "../controls/format";
import { StatusBadge } from "../controls/StatusBadge";
import { evidenceHref } from "../evidence/scope";
import { canWritePolicies, CHANGES_VIEW } from "../policies/format";
import { Badge } from "../ui/Badge";
import { formatTime } from "../time";
import { useDeviceClass } from "../ui/device";
import { Loading } from "../ui/Loading";
import { PageHeader } from "../ui/PageHeader";

function Decision({ control }: { control: ControlDetail }) {
  const change = control.change;
  if (!change) return null;
  return (
    <section aria-labelledby="decision-title" className="rounded border p-3">
      <h2 id="decision-title" className="card-title">
        {change.kind === "manual" ? "Met outside GitLab" : "Not applicable"}
      </h2>
      <dl className="mt-2">
        <div>
          <dt className="fw-medium">Justification</dt>
          <dd className="text-prewrap">{change.justification}</dd>
        </div>
        {change.evidence_ref && (
          <div>
            <dt className="fw-medium">Where the evidence is</dt>
            <dd>{change.evidence_ref}</dd>
          </div>
        )}
        {change.review_by && (
          <div>
            <dt className="fw-medium">Review by</dt>
            <dd>{change.review_by}</dd>
          </div>
        )}
      </dl>
    </section>
  );
}

function MappedRules({
  control,
  canWrite,
  open,
}: {
  control: ControlDetail;
  canWrite: boolean;
  open: (intent: ChangeIntent) => void;
}) {
  return (
    <section aria-labelledby="mapped-title">
      <h2 id="mapped-title" className="card-title">
        Rule types that count toward it
      </h2>
      {control.mapped.length === 0 ? (
        <p className="text-secondary">
          None. Map a rule type, or record how the control is met or why it does not apply.
        </p>
      ) : (
        <ul aria-label="Mapped rule types">
          {control.mapped.map((rule) => (
            <li key={rule.rule_type} className="rounded border p-3">
              <div className="d-flex flex-wrap align-items-start justify-content-between gap-2">
                <div>
                  <span className="fw-medium">{ruleTypeLabel(rule.rule_type)}</span>{" "}
                  <code className="small text-secondary">{rule.rule_type}</code>{" "}
                  <Badge tone="secondary">
                    {rule.source === "default" ? "Built in" : "Added by the organization"}
                  </Badge>
                  <p className="mt-1 text-secondary">
                    {rule.source === "default" ? rule.rationale : rule.justification}
                  </p>
                </div>
                {canWrite &&
                  (rule.source === "default" ? (
                    <button
                      type="button"
                      onClick={() => {
                        open({ kind: "unmap", ruleTypes: [rule.rule_type] });
                      }}
                      className="btn"
                    >
                      {`Remove ${ruleTypeLabel(rule.rule_type)}`}
                    </button>
                  ) : (
                    rule.change_id !== null && (
                      <button
                        type="button"
                        onClick={() => {
                          open({
                            kind: "withdraw",
                            supersedes: {
                              id: rule.change_id ?? 0,
                              text: `counts ${ruleTypeLabel(rule.rule_type)} toward the control`,
                            },
                          });
                        }}
                        className="btn"
                      >
                        {`Withdraw ${ruleTypeLabel(rule.rule_type)}`}
                      </button>
                    )
                  ))}
              </div>
            </li>
          ))}
        </ul>
      )}
      {(control.unmapped ?? []).length > 0 && (
        <>
          <h3 className="fw-semibold">Removed from the built-in mapping</h3>
          <ul aria-label="Removed rule types">
            {(control.unmapped ?? []).map((rule) => (
              <li key={rule.rule_type} className="rounded border p-3">
                <div className="d-flex flex-wrap align-items-start justify-content-between gap-2">
                  <div>
                    <span className="fw-medium">{ruleTypeLabel(rule.rule_type)}</span>{" "}
                    <code className="small text-secondary">{rule.rule_type}</code>
                    <p className="mt-1 text-secondary">{rule.justification}</p>
                  </div>
                  {canWrite && (
                    <button
                      type="button"
                      onClick={() => {
                        open({
                          kind: "withdraw",
                          supersedes: {
                            id: rule.change_id,
                            text: `removed ${ruleTypeLabel(rule.rule_type)} from the control`,
                          },
                        });
                      }}
                      className="btn"
                    >
                      {`Restore ${ruleTypeLabel(rule.rule_type)}`}
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function RuleResults({ control }: { control: ControlDetail }) {
  return (
    <section aria-labelledby="results-title">
      <h2 id="results-title" className="card-title">
        Results of the baselines in force
      </h2>
      {control.rules.length === 0 ? (
        <p className="text-secondary">No baseline in force has a rule of a mapped type.</p>
      ) : (
        <div className="card table-responsive">
          <table className="table table-vcenter card-table mb-0">
            <caption className="visually-hidden">Results per rule</caption>
            <thead>
              <tr>
                {["Rule", "Pass", "Fail", "Unknown", "Excepted", "Findings"].map((header) => (
                  <th key={header} scope="col">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {control.rules.map((rule) => {
                const policy = policyName(rule.policy);
                return (
                  <tr key={`${policy}/${rule.rule_id}`}>
                    <th scope="row" className="fw-normal">
                      <span className="fw-medium">{`${policy} / ${rule.rule_id}`}</span>
                      <span className="d-block small text-secondary">
                        {ruleTypeLabel(rule.rule_type)}
                      </span>
                    </th>
                    <td>{rule.counts.pass}</td>
                    <td>{rule.counts.fail}</td>
                    <td>{rule.counts.unknown}</td>
                    <td>{rule.counts.excepted}</td>
                    <td>
                      <Link to={findingsHref({ policy, rule: rule.rule_id })}>
                        {`Findings for ${rule.rule_id}`}
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function History({ changes }: { changes: MappingChange[] }) {
  return (
    <section aria-labelledby="history-title">
      <h2 id="history-title" className="card-title">
        Changes to its mapping
      </h2>
      {changes.length === 0 ? (
        <p className="text-secondary">No changes: the built-in mapping applies as published.</p>
      ) : (
        <div className="card table-responsive">
          <table className="table table-vcenter card-table mb-0">
            <caption className="visually-hidden">Mapping changes</caption>
            <thead>
              <tr>
                {["Change", "State", "Justification", "Proposed", "Review"].map((header) => (
                  <th key={header} scope="col">
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {changes.map((change) => (
                <tr key={change.id}>
                  <th scope="row" className="fw-normal">
                    {mappingChangeText(change)}
                    {change.review_by && (
                      <span className="d-block small text-secondary">{`Review by ${change.review_by}`}</span>
                    )}
                  </th>
                  <td>{changeStateLabels[change.state]}</td>
                  <td className="text-prewrap">
                    {change.justification}
                    {change.evidence_ref && (
                      <span className="d-block small text-secondary">{`Evidence: ${change.evidence_ref}`}</span>
                    )}
                  </td>
                  <td>
                    {changePerson(change.proposed_by)}
                    <span className="d-block small text-nowrap text-secondary">
                      {formatTime(change.proposed_at)}
                    </span>
                  </td>
                  <td>
                    {mappingDecisionText(change)}
                    {change.decision_note && (
                      <span className="d-block small text-secondary">{`“${change.decision_note}”`}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Actions({
  control,
  open,
}: {
  control: ControlDetail;
  open: (intent: ChangeIntent) => void;
}) {
  const mapped = new Set(control.mapped.map((rule) => rule.rule_type));
  const addable = ruleTypes.filter((type) => !mapped.has(type));
  const statusPending = control.changes.some(
    (change) =>
      change.state === "proposed" && (change.kind === "not_applicable" || change.kind === "manual"),
  );
  const decision = control.change;
  return (
    <div className="d-flex flex-wrap gap-2" role="group" aria-label="Change the mapping">
      {addable.length > 0 && (
        <button
          type="button"
          onClick={() => {
            open({ kind: "map", ruleTypes: addable });
          }}
          className="btn"
        >
          {kindLabels.map}
        </button>
      )}
      {decision === null && !statusPending && (
        <>
          <button
            type="button"
            onClick={() => {
              open({ kind: "not_applicable" });
            }}
            className="btn"
          >
            {kindLabels.not_applicable}
          </button>
          <button
            type="button"
            onClick={() => {
              open({ kind: "manual" });
            }}
            className="btn"
          >
            {kindLabels.manual}
          </button>
        </>
      )}
      {decision !== null && (
        <button
          type="button"
          onClick={() => {
            open({
              kind: "withdraw",
              supersedes: {
                id: decision.id,
                text:
                  decision.kind === "manual"
                    ? "records the control as met outside GitLab"
                    : "marks the control not applicable",
              },
            });
          }}
          className="btn"
        >
          {decision.kind === "manual"
            ? "Withdraw “met outside GitLab”"
            : "Withdraw “not applicable”"}
        </button>
      )}
    </div>
  );
}

/** One control: its status, the rules behind it, its results and the organization's changes. */
export function ControlDetailPage() {
  const { framework = "", controlId = "" } = useParams();
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const canWrite = canWritePolicies(user);
  const asOf = useAsOf();
  const past = asOf !== null;
  const device = useDeviceClass();
  const [reload, setReload] = useState(0);
  const [intent, setIntent] = useState<ChangeIntent | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const known = frameworkIds.find((id) => id === framework);
  const loaded = useLoad(
    `control:${framework}/${controlId}@${asOf ?? "now"}#${String(reload)}`,
    (signal) => getControl(framework, controlId, signal, asOf),
  );

  useEffect(() => {
    if (message) status.current?.focus();
  }, [message]);

  function done(change: MappingChange) {
    setIntent(null);
    setMessage(
      change.state === "proposed"
        ? `Proposed for review: ${mappingChangeText(change)}. It takes effect once someone other than you approves it.`
        : `Applied: ${mappingChangeText(change)}. Review was off, so it is in force now.`,
    );
    setReload((value) => value + 1);
  }

  const back = (
    <Link to={known && known !== "soc2" ? `${CONTROLS}?framework=${known}` : CONTROLS}>
      All controls
    </Link>
  );

  if (loaded.error) {
    return (
      <section aria-labelledby="page-title">
        <PageHeader pretitle={back} title={`${frameworkLabel(framework)} ${controlId}`} />
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      </section>
    );
  }
  const control = loaded.data;
  if (!control) {
    return (
      <section aria-labelledby="page-title">
        <PageHeader pretitle={back} title={`${frameworkLabel(framework)} ${controlId}`} />
        <Loading label="Loading the control…" />
      </section>
    );
  }
  const pending = control.changes.filter((change) => change.state === "proposed").length;

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={back}
        title={`${frameworkLabel(control.framework)} ${control.id}: ${control.title}`}
        subtitle={control.area}
        status={<StatusBadge status={control.status} />}
      />
      <p>{statusMeaning[control.status]}</p>
      <p>
        <Link
          to={evidenceHref({
            framework: control.framework as FrameworkId,
            control: control.id,
          })}
        >
          Export evidence for this control
        </Link>
      </p>
      {message && (
        <p ref={status} tabIndex={-1} role="status" className="alert alert-success text-body">
          {message}
        </p>
      )}
      {pending > 0 && (
        <p className="alert alert-info text-body">
          {`${String(pending)} ${pending === 1 ? "change to this control is" : "changes to this control are"} waiting for review. `}
          <Link to={CHANGES_VIEW}>Review changes</Link>
        </p>
      )}
      <AsOfResult answer={control} />
      <Decision control={control} />
      <fieldset disabled={past} className="d-contents">
        <MappedRules control={control} canWrite={canWrite} open={setIntent} />
      </fieldset>
      {canWrite && device !== "phone" && (
        <section aria-labelledby="actions-title" className="card card-body mb-3">
          <h2 id="actions-title" className="card-title">
            Adjust the mapping
          </h2>
          {past ? (
            <PastReadOnly />
          ) : (
            <p className="text-secondary">
              Every change needs a justification and is recorded. While review is required it takes
              effect only after someone else approves it.
            </p>
          )}
          {intent === null || past ? (
            <fieldset disabled={past} className="d-contents">
              <Actions control={control} open={setIntent} />
            </fieldset>
          ) : (
            <ChangeForm
              key={`${intent.kind}:${intent.ruleTypes?.join(",") ?? ""}:${String(intent.supersedes?.id ?? "")}`}
              framework={control.framework}
              controlId={control.id}
              intent={intent}
              onDone={done}
              onCancel={() => {
                setIntent(null);
              }}
            />
          )}
        </section>
      )}
      <RuleResults control={control} />
      <History changes={control.changes} />
    </section>
  );
}
