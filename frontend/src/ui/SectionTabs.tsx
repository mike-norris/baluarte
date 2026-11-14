import { NavLink } from "react-router";

export interface SectionTab {
  to: string;
  label: string;
  /** Match the path exactly rather than as a prefix. */
  end?: boolean;
}

/** Tabs for the views of one section, such as Manager or Baselines. */
export function SectionTabs({ label, tabs }: { label: string; tabs: readonly SectionTab[] }) {
  return (
    <nav aria-label={label} className="mb-3">
      <ul className="nav nav-tabs flex-nowrap overflow-auto">
        {tabs.map((tab) => (
          <li key={tab.to} className="nav-item">
            <NavLink to={tab.to} end={tab.end ?? false} className="nav-link text-nowrap">
              {tab.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
