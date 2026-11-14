import { Link } from "react-router";

import { changeHref, changesHref } from "../api/changes";
import type { Evaluation } from "../api/evaluations";
import { changeName, failingChanges, isChangeRule, kindLabels } from "./format";

/** For a change rule: the changes it failed on, each linked to its evidence. */
export function FailingChanges({ evaluation }: { evaluation: Evaluation }) {
  if (!isChangeRule(evaluation.rule_type)) return null;
  const { items, total } = failingChanges(evaluation.observed);
  const all = changesHref({ project: evaluation.project.gitlab_id, outcome: "fail" });
  return (
    <section aria-labelledby="failing-changes" className="card card-body mb-3">
      <h2 id="failing-changes" className="card-title">
        Failing changes
      </h2>
      {items.length === 0 ? (
        <p className="mt-2 text-secondary">
          {evaluation.outcome === "fail"
            ? "The result failed, but it names no changes. Open the project's changes to see them."
            : "No change failed this rule in the latest evaluation."}
        </p>
      ) : (
        <>
          <p className="mt-1 text-secondary">
            {total > items.length
              ? `The first ${String(items.length)} of ${String(total)} failing changes.`
              : `${String(total)} failing ${total === 1 ? "change" : "changes"}.`}
          </p>
          <ul className="mt-2">
            {items.map((item) => (
              <li key={item.id}>
                <Link to={changeHref(item.id)}>{changeName(item)}</Link>{" "}
                <span className="text-secondary">{`(${kindLabels[item.kind]})`}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="mt-3">
        <Link to={all}>Failing changes for this project</Link>
      </p>
    </section>
  );
}
