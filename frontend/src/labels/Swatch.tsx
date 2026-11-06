import { isHexColor } from "./format";

/**
 * A label colour: a small swatch beside its hex value. The text carries the meaning, so the
 * swatch itself is hidden from assistive technology and never stands alone.
 */
export function Swatch({ color }: { color: string | null | undefined }) {
  if (!color) return <span className="text-slate-600">None</span>;
  const valid = isHexColor(color);
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap font-mono text-xs">
      {valid && (
        <span
          aria-hidden="true"
          className="inline-block h-3.5 w-3.5 rounded-sm border border-slate-400"
          style={{ backgroundColor: color }}
        />
      )}
      {color.toLowerCase()}
    </span>
  );
}
