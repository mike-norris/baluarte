import {
  IconBan,
  IconCircleCheck,
  IconCircleDashed,
  IconCircleX,
  IconClockExclamation,
  IconHelpCircle,
  IconShieldHalf,
  type Icon,
} from "@tabler/icons-react";

import type { ControlStatus } from "../api/controls";
import { Badge, type Tone } from "../ui/Badge";
import { statusLabels, statusMeaning } from "./format";

const tone: Record<ControlStatus, Tone> = {
  met: "success",
  not_met: "danger",
  unknown: "secondary",
  no_results: "secondary",
  not_covered: "secondary",
  not_applicable: "secondary",
  manual: "indigo",
  review_overdue: "warning",
};

const icon: Record<ControlStatus, Icon> = {
  met: IconCircleCheck,
  not_met: IconCircleX,
  unknown: IconHelpCircle,
  no_results: IconHelpCircle,
  not_covered: IconCircleDashed,
  not_applicable: IconBan,
  manual: IconShieldHalf,
  review_overdue: IconClockExclamation,
};

/** A control's status as a word with colour and icon; the word alone carries the meaning. */
export function StatusBadge({ status }: { status: ControlStatus }) {
  return (
    <Badge tone={tone[status]} icon={icon[status]} title={statusMeaning[status]}>
      {statusLabels[status]}
    </Badge>
  );
}
