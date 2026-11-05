import { pastReadOnly } from "./format";

/** Why the actions beside it are turned off while the page shows the past. */
export function PastReadOnly({ id }: { id?: string }) {
  return (
    <p id={id} className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
      {pastReadOnly}
    </p>
  );
}
