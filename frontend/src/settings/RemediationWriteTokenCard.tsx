import {
  getRemediationWriteToken,
  putRemediationWriteToken,
  removeRemediationWriteToken,
} from "../api/remediation";
import { WriteTokenCard, type WriteTokenKind } from "./WriteTokenCard";

const REMEDIATION_TOKEN: WriteTokenKind = {
  id: "remediation-token",
  title: "Remediation write token",
  intro:
    "Used only to apply approved remediation plans: restrict a project's visibility and " +
    "protect a default branch. It never widens access, unprotects or deletes anything. It is " +
    "separate from the read-only token and the label write token. Setting, replacing and " +
    "removing it are recorded in the audit log.",
  writes: "Remediation writes",
  switchName: "BALUARTE_REMEDIATION_WRITES",
  help: (
    <>
      Never shown again once saved. Restricting visibility needs the Owner role and protecting a
      branch the Maintainer role, with the <code>api</code> scope. Use a group access token, limited
      to the groups Baluarte should fix, with an expiry date. Every read, including the checks
      before and after each change, keeps using the read-only token.
    </>
  ),
  removed:
    "Remediation write token removed. Remediation plans cannot be applied until a new one is set.",
  removeQuestion:
    "Remove the remediation write token? Approved remediation plans cannot be applied without one.",
  get: getRemediationWriteToken,
  put: putRemediationWriteToken,
  remove: removeRemediationWriteToken,
};

/** The separate, write-capable token that applies approved remediation plans (ADR-0012). */
export function RemediationWriteTokenCard({
  csrf,
  secretKey,
}: {
  csrf: string;
  secretKey: boolean | null;
}) {
  return <WriteTokenCard kind={REMEDIATION_TOKEN} csrf={csrf} secretKey={secretKey} />;
}
