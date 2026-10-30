import { DRY_RUN_NOTICE } from "./format";

/** Shown on every remediation screen: planning and approving never change GitLab. */
export function DryRunNotice() {
  return (
    <section
      aria-labelledby="dry-run"
      className="rounded border-2 border-amber-400 bg-amber-50 p-4 text-amber-950"
    >
      <h2 id="dry-run" className="text-lg font-semibold">
        Dry run only
      </h2>
      <p className="mt-1 text-sm">{DRY_RUN_NOTICE}</p>
    </section>
  );
}
