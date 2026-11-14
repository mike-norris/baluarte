import { IconHelpCircle } from "@tabler/icons-react";

import type { Part } from "./format";

/** A part of the change evidence; an unknown one is marked so it never reads as passed. */
export function PartText({ part }: { part: Part }) {
  return part.known ? (
    <span>{part.text}</span>
  ) : (
    <span className="d-inline-flex align-items-center gap-1 text-secondary">
      <IconHelpCircle size={14} aria-hidden="true" className="flex-shrink-0" />
      {part.text}
    </span>
  );
}
