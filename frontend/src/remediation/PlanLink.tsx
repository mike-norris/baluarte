import { Link } from "react-router";

import { CAN_PLAN } from "../api/remediation";
import { useAuth } from "../auth/context";
import { linkClass } from "../components/styles";

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
  if (session.status !== "signed_in" || !session.user.permissions.includes(CAN_PLAN)) {
    return null;
  }
  return (
    <p className="text-sm">
      <Link to={href} className={linkClass}>
        {label}
      </Link>
      {dropped.length > 0 && (
        <span className="text-slate-600">
          {` A plan cannot filter by ${dropped.join(" or ")}: it covers every open finding that matches the rest, and skips any that are not a known failure.`}
        </span>
      )}
    </p>
  );
}
