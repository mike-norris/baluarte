export interface NavItem {
  path: string;
  label: string;
  description: string;
  /** Shown only to users with this permission; the API enforces it regardless. */
  permission?: string;
}

/** The sections a user with these permissions sees. */
export function visibleNavItems(permissions: readonly string[]): NavItem[] {
  return navItems.filter((item) => !item.permission || permissions.includes(item.permission));
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
    path: "/changes",
    label: "Changes",
    description:
      "Merged merge requests and direct pushes, with their review and pipeline evidence.",
  },
  {
    path: "/labels",
    label: "Labels",
    description:
      "One label taxonomy for every group, where groups and projects drift from it, and reviewed fixes.",
  },
  {
    path: "/manager",
    label: "Manager",
    description:
      "Posture, change review, label drift and time rolled up by group or tag, with trends.",
    permission: "manager:read",
  },
  {
    path: "/exceptions",
    label: "Exceptions",
    description: "Time-limited, approved waivers of known failures.",
  },
  {
    path: "/remediation",
    label: "Remediation",
    description: "Dry-run plans to fix failing findings; nothing is changed in GitLab.",
  },
  {
    path: "/governance",
    label: "Governance",
    description: "Baselines Baluarte checks, and GitLab security policies GitLab enforces.",
  },
  {
    path: "/controls",
    label: "Controls",
    description: "SOC 2, ISO 27001 and NIST 800-53 controls the baseline rules support.",
  },
  {
    path: "/evidence",
    label: "Evidence",
    description: "Observation history and scoped evidence exports.",
  },
  {
    path: "/compliance",
    label: "Compliance",
    description: "How Baluarte itself meets SOC 2, ISO 27001 and NIST 800-53 controls.",
    permission: "compliance:read",
  },
  {
    path: "/audit-log",
    label: "Audit log",
    description: "Who did what in Baluarte, with each event's digest check.",
    permission: "audit_log:read",
  },
  {
    path: "/settings",
    label: "Settings",
    description: "Identity provider, GitLab connection and system information.",
    permission: "settings:manage",
  },
];
