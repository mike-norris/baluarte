import type { Icon } from "@tabler/icons-react";

/**
 * Tabler colour names for statuses. Subtle badges pass contrast in both themes; Tabler's `-lt`
 * badges and solid green or yellow do not, so they are not offered.
 */
export type Tone =
  | "success"
  | "danger"
  | "warning"
  | "info"
  | "primary"
  | "secondary"
  | "indigo"
  | "purple";

/** Indigo and purple have no subtle variant; their solid badges pass contrast. */
const SOLID: ReadonlySet<Tone> = new Set(["indigo", "purple"]);

export function badgeClass(tone: Tone): string {
  return SOLID.has(tone)
    ? `badge bg-${tone} text-${tone}-fg`
    : `badge bg-${tone}-subtle text-${tone}-emphasis border border-${tone}-subtle`;
}

interface BadgeProps {
  tone: Tone;
  children: string;
  icon?: Icon;
  /** A longer explanation, shown on hover. */
  title?: string;
}

/** A status as text with colour, and an icon where the meaning needs more than colour. */
export function Badge({ tone, children, icon: Glyph, title }: BadgeProps) {
  return (
    <span className={`${badgeClass(tone)} d-inline-flex align-items-center gap-1`} title={title}>
      {Glyph && <Glyph size={14} aria-hidden="true" />}
      {children}
    </span>
  );
}
