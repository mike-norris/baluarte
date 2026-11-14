import { Link } from "react-router";

import type { SkippedFinding } from "../api/remediation";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";
import { skipLabel } from "./format";

const columns: readonly Column<SkippedFinding>[] = [
  {
    key: "finding",
    header: "Finding",
    rowHeader: true,
    render: (item) => (
      <Link to={`/findings/${String(item.finding_id)}`} className="fw-medium">
        {`Finding ${String(item.finding_id)}`}
      </Link>
    ),
  },
  {
    key: "project",
    header: "Project",
    render: (item) => item.project_path ?? "Unknown",
  },
  {
    key: "rule",
    header: "Policy and rule",
    render: (item) =>
      item.policy_name && item.rule_id ? `${item.policy_name} / ${item.rule_id}` : "—",
    priority: 2,
  },
  {
    key: "why",
    header: "Why it was skipped",
    render: (item) => (
      <>
        <span className="fw-medium">{skipLabel(item.reason_code)}</span>
        {`: ${item.reason}`}
      </>
    ),
  },
];

/** Findings in scope that got no step, each with the API's reason. */
export function SkippedTable({ skipped, caption }: { skipped: SkippedFinding[]; caption: string }) {
  return (
    <ResponsiveTable
      caption={caption}
      columns={columns}
      rows={skipped}
      rowKey={(item) => `${String(item.finding_id)}:${item.reason_code}`}
    />
  );
}
