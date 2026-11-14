import { Link } from "react-router";

import { CAN_PLAN } from "../api/remediation";
import { useAsOf } from "../asof/context";
import { useAuth } from "../auth/context";

/** A link to the dry-run plan form, prefilled; shown only to users who may plan. */
export function PlanLink({
  href,
  label,
  dropped = [],
}: {
  href: string;
  label: string;
  dropped?: string[];
}) {
  const { session } = useAuth();
  const asOf = useAsOf();
  if (session.status !== "signed_in" || !session.user.permissions.includes(CAN_PLAN)) {
    return null;
  }
  if (asOf !== null) {
    return (
      <p className="text-secondary mb-0">
        {`${label}: not available while viewing the past. Plans are made from current findings, so go back to now to make one.`}
      </p>
    );
  }
  return (
    <p className="mb-0">
      <Link to={href}>
        {label}
      </Link>
      {dropped.length > 0 && (
        <span className="text-secondary">
          {` A plan cannot filter by ${dropped.join(" or ")}: it covers every open finding that matches the rest, and skips any that are not a known failure.`}
        </span>
      )}
    </p>
  );
}
