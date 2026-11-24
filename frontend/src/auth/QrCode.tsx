/** A QR code drawn as SVG from the server's module grid, so no image has to pass the CSP. */
export function QrCode({ rows, label }: { rows: string[]; label: string }) {
  const quiet = 4;
  const size = rows.length + quiet * 2;
  let path = "";
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x += 1) {
      if (row[x] === "1") path += `M${String(x + quiet)} ${String(y + quiet)}h1v1h-1z`;
    }
  });
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${String(size)} ${String(size)}`}
      width={Math.min(size * 6, 264)}
      height={Math.min(size * 6, 264)}
      shapeRendering="crispEdges"
      className="d-block mx-auto"
    >
      <rect width={size} height={size} fill="#fff" />
      <path d={path} fill="#000" />
    </svg>
  );
}
