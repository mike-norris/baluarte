import type { ReactNode } from "react";

interface DecisionBarProps {
  /** What deciding means here, or why no decision can be made. */
  summary: ReactNode;
  /** The decision buttons, already wrapped in CapabilityGate where needed. */
  children?: ReactNode;
}

/**
 * The decision for a reviewed item, kept in view at the bottom of the screen while the steps
 * scroll, instead of after them. Each button opens a Dialog that restates what is decided.
 */
export function DecisionBar({ summary, children }: DecisionBarProps) {
  return (
    <section aria-label="Decision" className="app-decision-bar card shadow mt-3">
      <div className="card-body d-flex flex-wrap align-items-center gap-3 py-2">
        <div className="flex-grow-1 small">{summary}</div>
        {children && <div className="btn-list">{children}</div>}
      </div>
    </section>
  );
}
