import type { ControlStatus } from "../api/controls";
import { statusClasses, statusLabels, statusMeaning } from "./format";

/** A control's status as text in a coloured border; the text alone carries the meaning. */
export function StatusBadge({ status }: { status: ControlStatus }) {
  return (
    <span
      title={statusMeaning[status]}
      className={`inline-block rounded border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${statusClasses[status]}`}
    >
      {statusLabels[status]}
    </span>
  );
}
