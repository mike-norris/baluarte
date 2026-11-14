import {
  IconAlertCircle,
  IconAlertOctagon,
  IconAlertTriangle,
  IconBan,
  IconCircleCheck,
  IconCircleX,
  IconHelpCircle,
  IconInfoCircle,
  IconShieldHalf,
} from "@tabler/icons-react";

import type { FindingState, Outcome } from "../api/evaluations";
import { Badge, type Tone } from "../ui/Badge";
import { findingStateLabels, outcomeLabels, outcomeMeaning, severityLabels } from "./format";

const outcomeTone: Record<Outcome, Tone> = {
  pass: "success",
  fail: "danger",
  unknown: "secondary",
  unsupported: "secondary",
  excepted: "indigo",
};

const outcomeIcon = {
  pass: IconCircleCheck,
  fail: IconCircleX,
  unknown: IconHelpCircle,
  unsupported: IconBan,
  excepted: IconShieldHalf,
} as const;

/**
 * An outcome as a word, colour and icon. Unless `brief`, unknown and unsupported also tell screen
 * readers what they mean.
 */
export function OutcomeBadge({ outcome, brief = false }: { outcome: Outcome; brief?: boolean }) {
  const doubtful = !brief && (outcome === "unknown" || outcome === "unsupported");
  return (
    <>
      <Badge
        tone={outcomeTone[outcome]}
        icon={outcomeIcon[outcome]}
        title={outcomeMeaning[outcome]}
      >
        {outcomeLabels[outcome]}
      </Badge>
      {doubtful && <span className="visually-hidden">: {outcomeMeaning[outcome]}</span>}
    </>
  );
}

/** Critical is a solid badge; the rest are subtle. Unrecognised severities show as given. */
export function SeverityBadge({ severity }: { severity: string }) {
  const label = severityLabels[severity] ?? severity;
  if (severity === "critical") {
    return (
      <span className="badge bg-danger text-danger-fg d-inline-flex align-items-center gap-1">
        <IconAlertOctagon size={14} aria-hidden="true" />
        {label}
      </span>
    );
  }
  if (severity === "high") {
    return (
      <Badge tone="danger" icon={IconAlertTriangle}>
        {label}
      </Badge>
    );
  }
  if (severity === "medium") {
    return (
      <Badge tone="warning" icon={IconAlertCircle}>
        {label}
      </Badge>
    );
  }
  return (
    <Badge tone="secondary" icon={IconInfoCircle}>
      {label}
    </Badge>
  );
}

const stateTone: Record<FindingState, Tone> = {
  open: "danger",
  resolved: "success",
  excepted: "indigo",
};

export function FindingStateBadge({ state }: { state: FindingState }) {
  return <Badge tone={stateTone[state]}>{findingStateLabels[state]}</Badge>;
}
