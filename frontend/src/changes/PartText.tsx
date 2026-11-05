import type { Part } from "./format";

/** A part of the change evidence; an unknown one is marked so it never reads as passed. */
export function PartText({ part }: { part: Part }) {
  return part.known ? (
    <span>{part.text}</span>
  ) : (
    <span className="rounded bg-amber-100 px-1 text-amber-950">{part.text}</span>
  );
}
