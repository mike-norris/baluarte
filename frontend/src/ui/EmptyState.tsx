import type { Icon } from "@tabler/icons-react";
import type { ReactNode } from "react";

interface EmptyStateProps {
  title: string;
  icon?: Icon;
  children?: ReactNode;
  action?: ReactNode;
}

/** What a list or page shows when there is nothing to show, and what to do about it. */
export function EmptyState({ title, icon: Glyph, children, action }: EmptyStateProps) {
  return (
    <div className="empty py-4">
      {Glyph && (
        <div className="empty-icon" aria-hidden="true">
          <Glyph size={40} />
        </div>
      )}
      <p className="empty-title">{title}</p>
      {children && <div className="empty-subtitle text-secondary">{children}</div>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}
