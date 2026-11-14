import { isHexColor } from "./format";

/**
 * A label colour: a small swatch beside its hex value. The text carries the meaning, so the
 * swatch itself is hidden from assistive technology and never stands alone.
 */
export function Swatch({ color }: { color: string | null | undefined }) {
  if (!color) return <span className="text-secondary">None</span>;
  const valid = isHexColor(color);
  return (
    <span className="d-inline-flex align-items-center gap-1 text-nowrap font-monospace small">
      {valid && (
        <span
          aria-hidden="true"
          className="d-inline-block rounded-1 border flex-shrink-0"
          style={{ backgroundColor: color, width: "0.875rem", height: "0.875rem" }}
        />
      )}
      {color.toLowerCase()}
    </span>
  );
}
