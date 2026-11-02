import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";

import { getPosture, type Posture } from "../api/compliance";
import { COMPLIANCE, frameworkList } from "./format";

// Posture changes with settings and attestations, not by the second; refresh now and then.
export const POSTURE_REFRESH_MS = 5 * 60_000;

/**
 * Shown to administrators and auditors on every page while Baluarte does not meet its own
 * compliance profile. Nothing is shown before a profile is chosen, or if posture cannot be read.
 */
export function ComplianceAlertBanner() {
  const [posture, setPosture] = useState<Posture | null>(null);
  const { pathname } = useLocation();

  useEffect(() => {
    const controller = new AbortController();
    const load = () => {
      getPosture(controller.signal).then(setPosture, () => {
        // The Compliance page reports failures; the banner stays quiet.
      });
    };
    load();
    const timer = window.setInterval(load, POSTURE_REFRESH_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  if (!posture?.configured || pathname === COMPLIANCE) return null;
  const { not_met: notMet, needs_attestation: unattested } = posture.summary;
  if (notMet + unattested === 0) return null;
  const parts = [
    notMet > 0 ? `${String(notMet)} not met` : null,
    unattested > 0 ? `${String(unattested)} awaiting attestation` : null,
  ].filter(Boolean);
  return (
    <div role="alert" className="border-b border-amber-300 bg-amber-50 text-sm text-amber-950">
      <div className="mx-auto max-w-6xl px-4 py-3">
        <p>
          <span className="font-semibold">
            {`Baluarte does not fully meet its compliance profile (${frameworkList(posture.frameworks)}): ${parts.join(", ")}.`}
          </span>{" "}
          <Link to={COMPLIANCE} className="underline">
            Review compliance
          </Link>
        </p>
      </div>
    </div>
  );
}
