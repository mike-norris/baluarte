import { Link } from "react-router";

import type { SkippedFinding } from "../api/remediation";
import { linkClass } from "../components/styles";
import { skipLabel } from "./format";

const cell = "px-3 py-2 align-top";

/** Findings in scope that got no step, each with the API's reason. */
export function SkippedTable({ skipped, caption }: { skipped: SkippedFinding[]; caption: string }) {
  return (
    <div className="mt-2 overflow-x-auto rounded border border-slate-200 bg-white text-slate-900">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
          <tr>
            <th scope="col" className={`${cell} font-medium`}>
              Finding
            </th>
            <th scope="col" className={`${cell} font-medium`}>
              Project
            </th>
            <th scope="col" className={`${cell} font-medium`}>
              Policy and rule
            </th>
            <th scope="col" className={`${cell} font-medium`}>
              Why it was skipped
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {skipped.map((item) => (
            <tr key={`${String(item.finding_id)}:${item.reason_code}`}>
              <th scope="row" className={`${cell} font-medium`}>
                <Link to={`/findings/${String(item.finding_id)}`} className={linkClass}>
                  {`Finding ${String(item.finding_id)}`}
                </Link>
              </th>
              <td className={cell}>{item.project_path ?? "Unknown"}</td>
              <td className={cell}>
                {item.policy_name && item.rule_id ? `${item.policy_name} / ${item.rule_id}` : "—"}
              </td>
              <td className={cell}>
                <span className="font-medium">{skipLabel(item.reason_code)}</span>
                {`: ${item.reason}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
