import { useId } from "react";
import { Link } from "react-router";

import type { Loaded } from "../api/useLoad";
import { controlPath } from "../api/controls";
import { frameworkLabel } from "../controls/format";
import {
  LABELS_TYPE,
  ruleTypeInfo,
  ruleTypeName,
  SECURITY_POLICY_TYPES,
  type Coverage,
  type RuleTypeCoverage,
} from "./coverage";

const statusChangeText = {
  not_applicable: "scoped out",
  manual: "met outside GitLab",
} as const;

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

/** Why a missing rule type may not be usable here, when the capabilities say so. */
function supportNote(item: RuleTypeCoverage): string | null {
  if (SECURITY_POLICY_TYPES.includes(item.rule_type) && item.supported !== true) {
    return "Needs GitLab Ultimate; this instance does not report security policies.";
  }
  if (item.supported === false) return "This GitLab instance does not support it.";
  return null;
}

/**
 * How much of what Baluarte can check is in force: rule types used by an applied baseline, the
 * ones missing with what each proves, and the controls left without automated evidence. Read
 * only, so it is the same on every device.
 */
export function CoverageCard({ coverage }: { coverage: Loaded<Coverage> }) {
  const id = useId();
  const data = coverage.data;

  let body;
  if (coverage.error) {
    body = <p className="text-secondary">Coverage is unavailable right now.</p>;
  } else if (!data) {
    body = (
      <p role="status" className="text-secondary">
        Loading coverage…
      </p>
    );
  } else {
    const checked = data.rule_types.filter((item) => item.rule_type !== LABELS_TYPE);
    const inForce = checked.filter((item) => item.in_force);
    const missing = checked.filter((item) => !item.in_force);
    const labels = data.rule_types.find((item) => item.rule_type === LABELS_TYPE);
    const controls = data.controls;
    body = (
      <div className="stack-3">
        <p className="fs-3 fw-semibold mb-0">
          {`${String(inForce.length)} of ${String(checked.length)} rule types in force`}
        </p>
        {missing.length === 0 ? (
          <p className="text-secondary">Every rule type is used by an applied baseline.</p>
        ) : (
          <div className="stack-1">
            <h3 id={`${id}-missing`} className="h4 mb-1">
              Not in force
            </h3>
            <ul aria-labelledby={`${id}-missing`} className="list-unstyled stack-2 mb-0">
              {missing.map((item) => {
                const note = supportNote(item);
                return (
                  <li key={item.rule_type}>
                    <span className="fw-medium">{ruleTypeName(item.rule_type)}</span>
                    <span className="d-block text-secondary">
                      {ruleTypeInfo[item.rule_type]?.proves ?? item.rule_type}
                    </span>
                    {note && <span className="d-block small text-secondary">{note}</span>}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        <p>
          <span className="fw-medium">{"Label consistency: "}</span>
          {labels?.in_force ? "in force" : "not in force"}
          <span className="d-block text-secondary">
            {data.label_taxonomy_version === null
              ? "It follows the label taxonomy, and there is none yet. "
              : `It follows label taxonomy v${String(data.label_taxonomy_version)}. `}
            <Link to="/labels">Labels</Link>
          </span>
        </p>
        {controls.length === 0 ? (
          <p className="text-secondary">
            Every mapped control has automated evidence from a rule in force.
          </p>
        ) : (
          <details>
            <summary className="fw-medium">
              {`${plural(controls.length, "control has", "controls have")} no automated evidence from Baluarte`}
            </summary>
            <p className="mt-2 text-secondary">
              No rule type mapped to them is in force. They still need attestation or other
              evidence.
            </p>
            <ul className="list-unstyled stack-1 mb-0">
              {controls.map((item) => (
                <li key={`${item.framework}:${item.control_id}`} className="text-break">
                  <Link to={controlPath(item.framework, item.control_id)}>
                    {`${frameworkLabel(item.framework)} ${item.control_id}`}
                  </Link>
                  {` ${item.title}`}
                  {item.status_change && (
                    <span className="text-secondary">{` (${statusChangeText[item.status_change]})`}</span>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby={`${id}-title`} className="card">
      <div className="card-header">
        <h2 id={`${id}-title`} className="card-title">
          Rule coverage
        </h2>
      </div>
      <div className="card-body">{body}</div>
    </section>
  );
}
