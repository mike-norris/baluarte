import { useId } from "react";

export interface Series {
  name: string;
  /** One value per point; null leaves a gap. */
  values: (number | null)[];
}

// Each series differs by dash and marker as well as colour, so none relies on colour alone.
const styles = [
  { color: "#1d4ed8", dash: "", marker: "circle" },
  { color: "#b45309", dash: "6 4", marker: "square" },
  { color: "#047857", dash: "2 3", marker: "triangle" },
  { color: "#9d174d", dash: "10 3 2 3", marker: "diamond" },
  { color: "#475569", dash: "1 2", marker: "circle" },
] as const;

const WIDTH = 640;
const HEIGHT = 220;
const PAD = { top: 12, right: 16, bottom: 28, left: 52 };

function Marker({ kind, x, y, color }: { kind: string; x: number; y: number; color: string }) {
  if (kind === "square") {
    return <rect x={x - 3.5} y={y - 3.5} width={7} height={7} fill={color} />;
  }
  if (kind === "triangle") {
    return (
      <polygon
        points={`${x},${y - 4.5} ${x + 4.5},${y + 3.5} ${x - 4.5},${y + 3.5}`}
        fill={color}
      />
    );
  }
  if (kind === "diamond") {
    return (
      <polygon points={`${x},${y - 5} ${x + 5},${y} ${x},${y + 5} ${x - 5},${y}`} fill={color} />
    );
  }
  return <circle cx={x} cy={y} r={3.5} fill={color} />;
}

/** Runs of consecutive values, so a gap is not drawn as a line. */
function runs(points: ({ x: number; y: number } | null)[]): { x: number; y: number }[][] {
  const found: { x: number; y: number }[][] = [];
  let current: { x: number; y: number }[] = [];
  for (const point of points) {
    if (point === null) {
      if (current.length > 0) found.push(current);
      current = [];
    } else current.push(point);
  }
  if (current.length > 0) found.push(current);
  return found;
}

/**
 * A line chart with its data as a table: the picture is a summary, the table is the record.
 * Values are formatted with ``format``; ``max`` fixes the top of the scale (1 for rates).
 */
export function LineChart({
  title,
  labels,
  series,
  format,
  max,
}: {
  title: string;
  labels: string[];
  series: Series[];
  format: (value: number) => string;
  max?: number;
}) {
  const id = useId();
  const values = series.flatMap((item) => item.values).filter((v): v is number => v !== null);
  const top = max ?? Math.max(1, ...values);
  const plotWidth = WIDTH - PAD.left - PAD.right;
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;
  const x = (index: number) =>
    PAD.left + (labels.length <= 1 ? plotWidth / 2 : (index / (labels.length - 1)) * plotWidth);
  const y = (value: number) => PAD.top + plotHeight - (value / top) * plotHeight;
  const ticks = [0, 0.5, 1].map((share) => share * top);
  const step = Math.max(1, Math.ceil(labels.length / 6));
  const latest = series
    .map((item) => {
      const last = [...item.values].reverse().find((value) => value !== null);
      return last === undefined || last === null ? null : `${item.name} ${format(last)}`;
    })
    .filter(Boolean)
    .join(", ");
  const summary =
    values.length === 0
      ? `${title}: no data in this period.`
      : `${title} from ${labels[0] ?? ""} to ${labels.at(-1) ?? ""}. Latest: ${latest}. The table below lists every value.`;

  return (
    <figure
      aria-labelledby={`${id}-title`}
      className="rounded border p-3"
    >
      <figcaption id={`${id}-title`} className="fw-medium">
        {title}
      </figcaption>
      <svg
        role="img"
        aria-label={summary}
        viewBox={`0 0 ${String(WIDTH)} ${String(HEIGHT)}`}
        className="h-auto w-100 text-secondary"
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={y(tick)}
              y2={y(tick)}
              stroke="currentColor"
              strokeOpacity={0.25}
            />
            <text
              x={PAD.left - 6}
              y={y(tick) + 4}
              textAnchor="end"
              fontSize={11}
              fill="currentColor"
            >
              {format(tick)}
            </text>
          </g>
        ))}
        {labels.map((label, index) =>
          index % step === 0 || index === labels.length - 1 ? (
            <text
              key={label}
              x={x(index)}
              y={HEIGHT - 8}
              textAnchor="middle"
              fontSize={11}
              fill="currentColor"
            >
              {label}
            </text>
          ) : null,
        )}
        {series.map((item, index) => {
          const style = styles[index % styles.length] ?? styles[0];
          const points = item.values.map((value, at) =>
            value === null ? null : { x: x(at), y: y(value) },
          );
          return (
            <g key={item.name}>
              {runs(points).map((run) => (
                <polyline
                  key={`${String(run[0]?.x)}`}
                  points={run.map((point) => `${String(point.x)},${String(point.y)}`).join(" ")}
                  fill="none"
                  stroke={style.color}
                  strokeWidth={2}
                  strokeDasharray={style.dash}
                />
              ))}
              {points.map((point, at) =>
                point ? (
                  <Marker
                    key={at}
                    kind={style.marker}
                    x={point.x}
                    y={point.y}
                    color={style.color}
                  />
                ) : null,
              )}
            </g>
          );
        })}
      </svg>
      {series.length > 1 && (
        <ul aria-hidden="true" className="d-flex flex-wrap gap-3 small text-secondary">
          {series.map((item, index) => {
            const style = styles[index % styles.length] ?? styles[0];
            return (
              <li key={item.name} className="d-flex align-items-center gap-1.5">
                <svg width="28" height="10" viewBox="0 0 28 10">
                  <line
                    x1={0}
                    x2={28}
                    y1={5}
                    y2={5}
                    stroke={style.color}
                    strokeWidth={2}
                    strokeDasharray={style.dash}
                  />
                  <Marker kind={style.marker} x={14} y={5} color={style.color} />
                </svg>
                {item.name}
              </li>
            );
          })}
        </ul>
      )}
      <details >
        <summary className="text-primary">{`${title} as a table`}</summary>
        <div className="mt-2 overflow-x-auto">
          <table className="table table-vcenter card-table mb-0">
            <caption className="visually-hidden">{title}</caption>
            <thead className="border-b">
              <tr>
                <th scope="col" className="px-2 py-1 fw-medium">
                  Period ending
                </th>
                {series.map((item) => (
                  <th key={item.name} scope="col" className="px-2 py-1 fw-medium">
                    {item.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody >
              {labels.map((label, at) => (
                <tr key={label}>
                  <th scope="row" className="px-2 py-1 fw-normal text-nowrap">
                    {label}
                  </th>
                  {series.map((item) => {
                    const value = item.values[at];
                    return (
                      <td key={item.name} className="px-2 py-1">
                        {value === null || value === undefined ? "No data" : format(value)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
