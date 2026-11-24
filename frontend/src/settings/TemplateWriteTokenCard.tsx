import {
  getTemplateWriteToken,
  putTemplateWriteToken,
  removeTemplateWriteToken,
} from "../api/changeSets";
import { WriteTokenCard, type WriteTokenKind } from "./WriteTokenCard";

const TEMPLATE_TOKEN: WriteTokenKind = {
  id: "template-token",
  title: "Template write token",
  intro:
    "Used only to apply approved template change sets: project visibility, default branch " +
    "protection and merge request approval settings. Unlike the remediation token, a change " +
    "set can widen visibility or remove a protection when two people approved that. It is " +
    "separate from the read-only token and the other write tokens. Setting, replacing and " +
    "removing it are recorded in the audit log.",
  writes: "Template writes",
  switchName: "BALUARTE_TEMPLATE_WRITES",
  help: (
    <>
      Never shown again once saved. Changing visibility needs the Owner role, and branch protection
      and approval settings the Maintainer role, with the <code>api</code> scope. Use a group access
      token, limited to the groups change sets may change, with an expiry date. Every read,
      including the checks before and after each change, keeps using the read-only token.
    </>
  ),
  removed: "Template write token removed. Change sets cannot be applied until a new one is set.",
  removeQuestion:
    "Remove the template write token? Approved change sets cannot be applied without one.",
  get: getTemplateWriteToken,
  put: putTemplateWriteToken,
  remove: removeTemplateWriteToken,
};

/** The separate, write-capable token that applies approved template change sets (M14.4). */
export function TemplateWriteTokenCard({
  csrf,
  secretKey,
}: {
  csrf: string;
  secretKey: boolean | null;
}) {
  return <WriteTokenCard kind={TEMPLATE_TOKEN} csrf={csrf} secretKey={secretKey} />;
}
