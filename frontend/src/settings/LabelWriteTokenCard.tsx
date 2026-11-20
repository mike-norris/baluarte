import { getWriteToken, putWriteToken, removeWriteToken } from "../api/labels";
import { WriteTokenCard, type WriteTokenKind } from "./WriteTokenCard";

export { MAX_TOKEN } from "./WriteTokenCard";

const LABEL_TOKEN: WriteTokenKind = {
  id: "label-token",
  title: "Label write token",
  intro:
    "Used only to create, rename or update labels from an approved label plan, and to set " +
    "labels and estimates from an approved issue plan. It never deletes. Setting, replacing " +
    "and removing it are recorded in the audit log.",
  writes: "Label writes",
  switchName: "BALUARTE_LABEL_WRITES",
  help: (
    <>
      Never shown again once saved. Use a group access token with the Maintainer role and the{" "}
      <code>api</code> scope, limited to the groups whose labels Baluarte should fix, with an expiry
      date. It is used only to apply approved label plans; every other read keeps using the
      read-only token.
    </>
  ),
  removed: "Write token removed. Label plans cannot be applied until a new one is set.",
  removeQuestion: "Remove the write token? Approved label plans cannot be applied without one.",
  get: getWriteToken,
  put: putWriteToken,
  remove: removeWriteToken,
};

/** The separate, write-capable token that applies approved label plans (ADR-0009). */
export function LabelWriteTokenCard({
  csrf,
  secretKey,
}: {
  csrf: string;
  secretKey: boolean | null;
}) {
  return <WriteTokenCard kind={LABEL_TOKEN} csrf={csrf} secretKey={secretKey} />;
}
