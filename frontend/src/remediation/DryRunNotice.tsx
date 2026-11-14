import { DRY_RUN_NOTICE } from "./format";

/** Shown on every remediation screen: planning and approving never change GitLab. */
export function DryRunNotice() {
  return (
    <section aria-labelledby="dry-run" className="alert alert-warning text-body">
      <h2 id="dry-run" className="alert-title h4">
        Dry run only
      </h2>
      <p className="mb-0">{DRY_RUN_NOTICE}</p>
    </section>
  );
}
