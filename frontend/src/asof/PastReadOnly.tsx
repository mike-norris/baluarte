import { pastReadOnly } from "./format";

/** Why the actions beside it are turned off while the page shows the past. */
export function PastReadOnly({ id }: { id?: string }) {
  return (
    <p id={id} className="alert alert-warning text-body">
      {pastReadOnly}
    </p>
  );
}
