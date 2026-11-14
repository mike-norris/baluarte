import type { ReactNode } from "react";

interface PageHeaderProps {
  title: ReactNode;
  /** The small caption above the title, such as the sidebar group. */
  pretitle?: ReactNode;
  /** One line under the title. */
  subtitle?: ReactNode;
  /** Badges beside the title, such as a plan's state. */
  status?: ReactNode;
  /** Buttons and links on the right; they wrap under the title on narrow screens. */
  actions?: ReactNode;
}

/** A page's heading row. The title is the page's only level-1 heading, `#page-title`. */
export function PageHeader({ title, pretitle, subtitle, status, actions }: PageHeaderProps) {
  return (
    <div className="page-header d-print-none mt-0 mb-3">
      <div className="row g-2 align-items-center">
        <div className="col">
          {pretitle && <div className="page-pretitle">{pretitle}</div>}
          <div className="d-flex flex-wrap align-items-center gap-2">
            <h1 id="page-title" className="page-title">
              {title}
            </h1>
            {status}
          </div>
          {subtitle && <p className="text-secondary mt-1 mb-0">{subtitle}</p>}
        </div>
        {actions && (
          <div className="col-12 col-md-auto ms-md-auto">
            <div className="btn-list">{actions}</div>
          </div>
        )}
      </div>
    </div>
  );
}
