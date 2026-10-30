export interface NavItem {
  path: string;
  label: string;
  description: string;
}

/** Primary sections, in navigation order. Each maps to a v0.1 milestone in docs/ROADMAP.md. */
export const navItems: readonly NavItem[] = [
  {
    path: "/",
    label: "Dashboard",
    description: "Compliance posture across groups and projects.",
  },
  {
    path: "/inventory",
    label: "Inventory",
    description: "GitLab groups, subgroups and projects with collection freshness.",
  },
  {
    path: "/findings",
    label: "Findings",
    description: "Rule outcomes with expected and observed values.",
  },
  {
    path: "/exceptions",
    label: "Exceptions",
    description: "Time-limited, approved waivers of known failures.",
  },
  {
    path: "/policies",
    label: "Policies",
    description: "Versioned YAML baselines and their scope.",
  },
  {
    path: "/evidence",
    label: "Evidence",
    description: "Observation history and scoped evidence exports.",
  },
  {
    path: "/settings",
    label: "Settings",
    description: "GitLab connection and administration.",
  },
];
