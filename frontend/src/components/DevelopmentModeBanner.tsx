import { IconFlask } from "@tabler/icons-react";

/**
 * Shown on every page while the deployment runs in development mode (``BALUARTE_DEVELOPMENT_MODE``):
 * two-person review may then be turned off whatever the compliance profile, so changes can be
 * self-approved, and nothing this instance records should be handed to an auditor.
 */
export function DevelopmentModeBanner() {
  return (
    <div role="status" className="bg-purple text-purple-fg small py-1">
      <div className="container-fluid d-flex align-items-start gap-2">
        <IconFlask size={16} className="flex-shrink-0 mt-1" aria-hidden="true" />
        <p className="mb-0">
          <strong>Development mode.</strong>{" "}
          <span className="d-none d-md-inline">
            Two-person review can be turned off in Settings whatever the compliance profile
            requires, and changes are then self-approved.{" "}
          </span>
          Evidence from this instance is not audit evidence.
        </p>
      </div>
    </div>
  );
}
