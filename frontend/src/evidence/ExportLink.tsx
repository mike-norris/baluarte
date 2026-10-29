import { Link } from "react-router";

import { useAuth } from "../auth/context";
import { linkClass } from "../components/styles";

/** A link to the export form, prefilled; shown only to users who may export. */
export function ExportLink({
  href,
  label,
  dropped = [],
}: {
  href: string;
  label: string;
  dropped?: string[];
}) {
  const { session } = useAuth();
  if (session.status !== "signed_in" || !session.user.permissions.includes("evidence:export")) {
    return null;
  }
  return (
    <p className="text-sm">
      <Link to={href} className={linkClass}>
        {label}
      </Link>
      {dropped.length > 0 && (
        <span className="text-slate-600">
          {` The export cannot filter by ${dropped.join(" or ")}, so it includes every value.`}
        </span>
      )}
    </p>
  );
}
