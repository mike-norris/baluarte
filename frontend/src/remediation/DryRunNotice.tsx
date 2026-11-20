import { DRY_RUN_NOTICE } from "./format";

/** Shown on every remediation screen: only applying an approved plan changes GitLab. */
export function DryRunNotice() {
  return (
    <section aria-labelledby="dry-run" className="alert alert-warning text-body">
      <h2 id="dry-run" className="alert-title h4">
        Plans change GitLab only when applied
      </h2>
      <p className="mb-0">{DRY_RUN_NOTICE}</p>
    </section>
  );
}
