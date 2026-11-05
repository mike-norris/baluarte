import { Link } from "react-router";

import { changeHref, changesHref } from "../api/changes";
import type { Evaluation } from "../api/evaluations";
import { linkClass } from "../components/styles";
import { changeName, failingChanges, isChangeRule, kindLabels } from "./format";

/** For a change rule: the changes it failed on, each linked to its evidence. */
export function FailingChanges({ evaluation }: { evaluation: Evaluation }) {
  if (!isChangeRule(evaluation.rule_type)) return null;
  const { items, total } = failingChanges(evaluation.observed);
  const all = changesHref({ project: evaluation.project.gitlab_id, outcome: "fail" });
  return (
    <section
      aria-labelledby="failing-changes"
      className="rounded border border-slate-200 bg-white p-4"
    >
      <h2 id="failing-changes" className="text-lg font-semibold">
        Failing changes
      </h2>
      {items.length === 0 ? (
        <p className="mt-2 text-sm text-slate-700">
          {evaluation.outcome === "fail"
            ? "The result failed, but it names no changes. Open the project's changes to see them."
            : "No change failed this rule in the latest evaluation."}
        </p>
      ) : (
        <>
          <p className="mt-1 text-sm text-slate-600">
            {total > items.length
              ? `The first ${String(items.length)} of ${String(total)} failing changes.`
              : `${String(total)} failing ${total === 1 ? "change" : "changes"}.`}
          </p>
          <ul className="mt-2 space-y-1 text-sm">
            {items.map((item) => (
              <li key={item.id}>
                <Link to={changeHref(item.id)} className={linkClass}>
                  {changeName(item)}
                </Link>{" "}
                <span className="text-slate-600">{`(${kindLabels[item.kind]})`}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="mt-3 text-sm">
        <Link to={all} className={linkClass}>
          Failing changes for this project
        </Link>
      </p>
    </section>
  );
}
