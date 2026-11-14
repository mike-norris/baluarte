import type { ReactNode } from "react";
import { Link } from "react-router";

interface KpiCardProps {
  label: string;
  value: ReactNode;
  /** A line under the value: what it counts, or a caveat. */
  footnote?: ReactNode;
  /** The page with the detail; the whole card links there. */
  href?: string;
}

/** One headline figure on a dashboard. */
export function KpiCard({ label, value, footnote, href }: KpiCardProps) {
  const body = (
    <div className="card-body">
      <div className="subheader">{label}</div>
      <div className="h1 mb-0 mt-1 tabular-nums">{value}</div>
      {footnote && <div className="text-secondary small mt-1">{footnote}</div>}
    </div>
  );
  return (
    <div className="card card-sm h-100">
      {href ? (
        <Link to={href} className="text-reset text-decoration-none d-block h-100">
          {body}
        </Link>
      ) : (
        body
      )}
    </div>
  );
}
