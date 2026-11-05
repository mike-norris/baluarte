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
import { linkClass, secondaryClass } from "../components/styles";
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
import { formatTime } from "../time";

const cell = "px-3 py-2 align-top";

function Decision({ control }: { control: ControlDetail }) {
  const change = control.change;
  if (!change) return null;
  return (
    <section
      aria-labelledby="decision-title"
      className="rounded border border-slate-200 bg-white p-4 text-sm"
    >
      <h2 id="decision-title" className="text-lg font-semibold">
        {change.kind === "manual" ? "Met outside GitLab" : "Not applicable"}
      </h2>
      <dl className="mt-2 space-y-2">
        <div>
          <dt className="font-medium">Justification</dt>
          <dd className="whitespace-pre-wrap">{change.justification}</dd>
        </div>
        {change.evidence_ref && (
          <div>
            <dt className="font-medium">Where the evidence is</dt>
            <dd>{change.evidence_ref}</dd>
          </div>
        )}
        {change.review_by && (
          <div>
            <dt className="font-medium">Review by</dt>
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
    <section aria-labelledby="mapped-title" className="space-y-2">
      <h2 id="mapped-title" className="text-lg font-semibold">
        Rule types that count toward it
      </h2>
      {control.mapped.length === 0 ? (
        <p className="text-sm text-slate-600">
          None. Map a rule type, or record how the control is met or why it does not apply.
        </p>
      ) : (
        <ul className="space-y-2" aria-label="Mapped rule types">
          {control.mapped.map((rule) => (
            <li key={rule.rule_type} className="rounded border border-slate-200 bg-white p-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="text-sm">
                  <span className="font-medium">{ruleTypeLabel(rule.rule_type)}</span>{" "}
                  <code className="text-xs text-slate-600">{rule.rule_type}</code>{" "}
                  <span className="rounded bg-slate-100 px-1 text-xs text-slate-700">
                    {rule.source === "default" ? "Built in" : "Added by the organization"}
                  </span>
                  <p className="mt-1 text-slate-700">
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
                      className={secondaryClass}
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
                        className={secondaryClass}
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
          <h3 className="text-base font-semibold">Removed from the built-in mapping</h3>
          <ul className="space-y-2" aria-label="Removed rule types">
            {(control.unmapped ?? []).map((rule) => (
              <li key={rule.rule_type} className="rounded border border-slate-200 bg-white p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="text-sm">
                    <span className="font-medium">{ruleTypeLabel(rule.rule_type)}</span>{" "}
                    <code className="text-xs text-slate-600">{rule.rule_type}</code>
                    <p className="mt-1 text-slate-700">{rule.justification}</p>
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
                      className={secondaryClass}
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
    <section aria-labelledby="results-title" className="space-y-2">
      <h2 id="results-title" className="text-lg font-semibold">
        Results of the baselines in force
      </h2>
      {control.rules.length === 0 ? (
        <p className="text-sm text-slate-600">
          No baseline in force has a rule of a mapped type.
        </p>
      ) : (
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Results per rule</caption>
            <thead className="border-b border-slate-200 bg-slate-50">
              <tr>
                {["Rule", "Pass", "Fail", "Unknown", "Excepted", "Findings"].map((header) => (
                  <th key={header} scope="col" className={`${cell} font-medium`}>
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {control.rules.map((rule) => {
                const policy = policyName(rule.policy);
                return (
                  <tr key={`${policy}/${rule.rule_id}`}>
                    <th scope="row" className={`${cell} font-normal`}>
                      <span className="font-medium">{`${policy} / ${rule.rule_id}`}</span>
                      <span className="block text-xs text-slate-600">
                        {ruleTypeLabel(rule.rule_type)}
                      </span>
                    </th>
                    <td className={cell}>{rule.counts.pass}</td>
                    <td className={cell}>{rule.counts.fail}</td>
                    <td className={cell}>{rule.counts.unknown}</td>
                    <td className={cell}>{rule.counts.excepted}</td>
                    <td className={cell}>
                      <Link
                        to={findingsHref({ policy, rule: rule.rule_id })}
                        className={linkClass}
                      >
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
    <section aria-labelledby="history-title" className="space-y-2">
      <h2 id="history-title" className="text-lg font-semibold">
        Changes to its mapping
      </h2>
      {changes.length === 0 ? (
        <p className="text-sm text-slate-600">
          No changes: the built-in mapping applies as published.
        </p>
      ) : (
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Mapping changes</caption>
            <thead className="border-b border-slate-200 bg-slate-50">
              <tr>
                {["Change", "State", "Justification", "Proposed", "Review"].map((header) => (
                  <th key={header} scope="col" className={`${cell} font-medium`}>
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {changes.map((change) => (
                <tr key={change.id}>
                  <th scope="row" className={`${cell} font-normal`}>
                    {mappingChangeText(change)}
                    {change.review_by && (
                      <span className="block text-xs text-slate-600">{`Review by ${change.review_by}`}</span>
                    )}
                  </th>
                  <td className={cell}>{changeStateLabels[change.state]}</td>
                  <td className={`${cell} whitespace-pre-wrap`}>
                    {change.justification}
                    {change.evidence_ref && (
                      <span className="block text-xs text-slate-600">{`Evidence: ${change.evidence_ref}`}</span>
                    )}
                  </td>
                  <td className={cell}>
                    {changePerson(change.proposed_by)}
                    <span className="block text-xs whitespace-nowrap text-slate-600">
                      {formatTime(change.proposed_at)}
                    </span>
                  </td>
                  <td className={cell}>
                    {mappingDecisionText(change)}
                    {change.decision_note && (
                      <span className="block text-xs text-slate-600">{`“${change.decision_note}”`}</span>
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
    <div className="flex flex-wrap gap-2" role="group" aria-label="Change the mapping">
      {addable.length > 0 && (
        <button
          type="button"
          onClick={() => {
            open({ kind: "map", ruleTypes: addable });
          }}
          className={secondaryClass}
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
            className={secondaryClass}
          >
            {kindLabels.not_applicable}
          </button>
          <button
            type="button"
            onClick={() => {
              open({ kind: "manual" });
            }}
            className={secondaryClass}
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
          className={secondaryClass}
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
    <Link
      to={known && known !== "soc2" ? `${CONTROLS}?framework=${known}` : CONTROLS}
      className={linkClass}
    >
      All controls
    </Link>
  );

  if (loaded.error) {
    return (
      <section aria-labelledby="page-title" className="space-y-4">
        {back}
        <h1 id="page-title" className="text-2xl font-semibold">
          {`${frameworkLabel(framework)} ${controlId}`}
        </h1>
        <ErrorMessage error={loaded.error} retry={loaded.retry} />
      </section>
    );
  }
  const control = loaded.data;
  if (!control) {
    return (
      <p role="status" className="text-slate-600">
        Loading the control…
      </p>
    );
  }
  const pending = control.changes.filter((change) => change.state === "proposed").length;

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      {back}
      <div className="space-y-2">
        <h1 id="page-title" className="text-2xl font-semibold">
          {`${frameworkLabel(control.framework)} ${control.id}: ${control.title}`}
        </h1>
        <p className="text-sm text-slate-600">{control.area}</p>
        <p className="flex flex-wrap items-center gap-2 text-sm">
          <StatusBadge status={control.status} />
          <span>{statusMeaning[control.status]}</span>
        </p>
        <p className="text-sm">
          <Link
            to={evidenceHref({
              framework: control.framework as FrameworkId,
              control: control.id,
            })}
            className={linkClass}
          >
            Export evidence for this control
          </Link>
        </p>
      </div>
      {message && (
        <p
          ref={status}
          tabIndex={-1}
          role="status"
          className="rounded border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-950"
        >
          {message}
        </p>
      )}
      {pending > 0 && (
        <p className="rounded border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
          {`${String(pending)} ${pending === 1 ? "change to this control is" : "changes to this control are"} waiting for review. `}
          <Link to={CHANGES_VIEW} className={linkClass}>
            Review changes
          </Link>
        </p>
      )}
      <AsOfResult answer={control} />
      <Decision control={control} />
      <fieldset disabled={past} className="contents">
        <MappedRules control={control} canWrite={canWrite} open={setIntent} />
      </fieldset>
      {canWrite && (
        <section aria-labelledby="actions-title" className="space-y-3">
          <h2 id="actions-title" className="text-lg font-semibold">
            Adjust the mapping
          </h2>
          {past ? (
            <PastReadOnly />
          ) : (
            <p className="max-w-3xl text-sm text-slate-700">
              Every change needs a justification and is recorded. While review is required it takes
              effect only after someone else approves it.
            </p>
          )}
          {intent === null || past ? (
            <fieldset disabled={past} className="contents">
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
