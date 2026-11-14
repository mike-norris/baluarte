import type { ReactNode } from "react";

import { useAsOf } from "../asof/context";
import { useAuth } from "../auth/context";
import { useDeviceClass } from "./device";

/**
 * What a control does. On phones only decisions (approve or reject) are offered; everything
 * else is read-only there.
 */
export type Capability = "decide" | "write" | "apply" | "bulk" | "export" | "settings";

interface CapabilityGateProps {
  capability: Capability;
  /** Hidden entirely without this permission. */
  permission?: string;
  children: ReactNode;
  /** Shown instead of the control where it is not available; nothing by default. */
  fallback?: ReactNode;
}

export const LARGER_SCREEN = "Available on a larger screen.";

/**
 * Shows a control only where it may be used: with the permission, outside a past-date view, and
 * on phones only for decisions. This is a usability rule, not a security control; the API
 * enforces permissions and separation of duties regardless.
 */
export function CapabilityGate({ capability, permission, children, fallback }: CapabilityGateProps) {
  const { session } = useAuth();
  const asOf = useAsOf();
  const device = useDeviceClass();
  if (permission) {
    if (session.status !== "signed_in" || !session.user.permissions.includes(permission)) {
      return null;
    }
  }
  if (asOf !== null) return <>{fallback ?? null}</>;
  if (device === "phone" && capability !== "decide") {
    return <>{fallback ?? <p className="text-secondary small mb-0">{LARGER_SCREEN}</p>}</>;
  }
  return <>{children}</>;
}
