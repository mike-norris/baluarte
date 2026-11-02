/** A line diff of two baseline sources, for reviewing a proposed version. */

export interface DiffLine {
  kind: "same" | "added" | "removed";
  text: string;
}

// Baselines are at most 64 KiB; past this many line pairs the table would be too large, so
// the whole old text is shown removed and the new text added.
const MAX_CELLS = 4_000_000;

/** Longest-common-subsequence diff of ``before`` and ``after``, by line. */
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  if (a.length * b.length > MAX_CELLS) {
    return [
      ...a.map((text): DiffLine => ({ kind: "removed", text })),
      ...b.map((text): DiffLine => ({ kind: "added", text })),
    ];
  }
  // LCS length of a[i:] and b[j:], in a flat table with b.length + 1 columns.
  const width = b.length + 1;
  const lengths = new Uint32Array((a.length + 1) * width);
  const length = (i: number, j: number): number => lengths[i * width + j] ?? 0;
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lengths[i * width + j] =
        a[i] === b[j] ? length(i + 1, j + 1) + 1 : Math.max(length(i + 1, j), length(i, j + 1));
    }
  }
  const lines: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    const left = a[i] ?? "";
    const right = b[j] ?? "";
    if (left === right) {
      lines.push({ kind: "same", text: left });
      i++;
      j++;
    } else if (length(i + 1, j) >= length(i, j + 1)) {
      lines.push({ kind: "removed", text: left });
      i++;
    } else {
      lines.push({ kind: "added", text: right });
      j++;
    }
  }
  lines.push(...a.slice(i).map((text): DiffLine => ({ kind: "removed", text })));
  lines.push(...b.slice(j).map((text): DiffLine => ({ kind: "added", text })));
  return lines;
}
