import {
  IconAlertTriangle,
  IconCertificate,
  IconChartHistogram,
  IconChecklist,
  IconClockCheck,
  IconFileExport,
  IconGitMerge,
  IconHistoryToggle,
  IconLayoutDashboard,
  IconScale,
  IconSettings,
  IconShieldHalf,
  IconSitemap,
  IconTag,
  IconTags,
  IconTool,
  type Icon,
} from "@tabler/icons-react";

export type NavGroup = "Overview" | "Observe" | "Act" | "Govern" | "Evidence" | "Admin";

export interface NavItem {
  path: string;
  /** The name in the sidebar. */
  label: string;
  /** The page's own heading, where it differs from the sidebar name. */
  title?: string;
  description: string;
  group: NavGroup;
  icon: Icon;
  /** Shown only to users with this permission; the API enforces it regardless. */
  permission?: string;
  /** False for pages routed on their own rather than as a top-level section. */
  section?: false;
  /** Shown in the phone's bottom bar. */
  phoneBar?: true;
}

/** Sidebar groups, in order. */
export const navGroups: readonly NavGroup[] = [
  "Overview",
  "Observe",
  "Act",
  "Govern",
  "Evidence",
  "Admin",
];

/** The page heading for a navigation item. */
export function navTitle(item: NavItem): string {
  return item.title ?? item.label;
}

/** The items a user with these permissions sees. */
export function visibleNavItems(permissions: readonly string[]): NavItem[] {
  return navItems.filter((item) => !item.permission || permissions.includes(item.permission));
}

/** Navigation items in sidebar order. Sections each map to a v0.1 milestone in docs/ROADMAP.md. */
export const navItems: readonly NavItem[] = [
  {
    path: "/",
    label: "Dashboard",
    description: "Compliance posture across groups and projects.",
    group: "Overview",
    icon: IconLayoutDashboard,
    phoneBar: true,
  },
  {
    path: "/manager",
    label: "Manager",
    description:
      "Posture, change review, label drift and time rolled up by group or tag, with trends.",
    group: "Overview",
    icon: IconChartHistogram,
    permission: "manager:read",
    phoneBar: true,
  },
  {
    path: "/inventory",
    label: "Inventory",
    description: "GitLab groups, subgroups and projects with collection freshness.",
    group: "Observe",
    icon: IconSitemap,
  },
  {
    path: "/findings",
    label: "Findings",
    description: "Rule outcomes with expected and observed values.",
    group: "Observe",
    icon: IconAlertTriangle,
    phoneBar: true,
  },
  {
    path: "/changes",
    label: "Code changes",
    title: "Changes",
    description:
      "Merged merge requests and direct pushes, with their review and pipeline evidence.",
    group: "Observe",
    icon: IconGitMerge,
  },
  {
    path: "/controls",
    label: "Controls",
    description: "SOC 2, ISO 27001 and NIST 800-53 controls the baseline rules support.",
    group: "Observe",
    icon: IconChecklist,
  },
  {
    path: "/remediation",
    label: "Remediation plans",
    title: "Remediation",
    description: "Dry-run plans to fix failing findings; nothing is changed in GitLab.",
    group: "Act",
    icon: IconTool,
  },
  {
    path: "/exceptions",
    label: "Exceptions",
    description: "Time-limited, approved waivers of known failures.",
    group: "Act",
    icon: IconShieldHalf,
  },
  {
    path: "/labels/plans",
    label: "Label plans",
    description: "Reviewed plans that bring GitLab labels in line with the taxonomy.",
    group: "Act",
    icon: IconTags,
    section: false,
  },
  {
    path: "/time/issue-plans",
    label: "Issue time plans",
    title: "Closed issue time",
    description: "Reviewed plans that label closed issues missing time and set their estimates.",
    group: "Act",
    icon: IconClockCheck,
    section: false,
  },
  {
    path: "/governance",
    label: "Baselines",
    title: "Governance",
    description: "Baselines Baluarte checks, and GitLab security policies GitLab enforces.",
    group: "Govern",
    icon: IconScale,
  },
  {
    path: "/labels",
    label: "Label taxonomy",
    title: "Labels",
    description:
      "One label taxonomy for every group, where groups and projects drift from it, and reviewed fixes.",
    group: "Govern",
    icon: IconTag,
  },
  {
    path: "/evidence",
    label: "Evidence exports",
    title: "Evidence",
    description: "Observation history and scoped evidence exports.",
    group: "Evidence",
    icon: IconFileExport,
  },
  {
    path: "/audit-log",
    label: "Audit log",
    description: "Who did what in Baluarte, with each event's digest check.",
    group: "Evidence",
    icon: IconHistoryToggle,
    permission: "audit_log:read",
  },
  {
    path: "/compliance",
    label: "Baluarte compliance",
    title: "Compliance",
    description: "How Baluarte itself meets SOC 2, ISO 27001 and NIST 800-53 controls.",
    group: "Evidence",
    icon: IconCertificate,
    permission: "compliance:read",
  },
  {
    path: "/settings",
    label: "Settings",
    description: "Identity provider, GitLab connection and system information.",
    group: "Admin",
    icon: IconSettings,
    permission: "settings:manage",
  },
];

/** Top-level sections, each routed from this list. */
export const sectionItems: readonly NavItem[] = navItems.filter((item) => item.section !== false);
