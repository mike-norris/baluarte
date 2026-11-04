/**
 * Shown on every page while the deployment runs in development mode (``BALUARTE_DEVELOPMENT_MODE``):
 * two-person review may then be turned off whatever the compliance profile, so changes can be
 * self-approved, and nothing this instance records should be handed to an auditor.
 */
export function DevelopmentModeBanner() {
  return (
    <div role="status" className="border-b border-violet-300 bg-violet-50 text-sm text-violet-950">
      <div className="mx-auto max-w-6xl px-4 py-2">
        <p>
          <span className="font-semibold">Development mode.</span> Two-person review can be turned
          off in Settings whatever the compliance profile requires, and changes are then
          self-approved. Evidence from this instance is not audit evidence.
        </p>
      </div>
    </div>
  );
}
