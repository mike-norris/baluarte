interface LoadingProps {
  /** Read out to screen readers; the skeleton itself is hidden from them. */
  label?: string;
  /** Skeleton rows to draw. */
  rows?: number;
}

/** A loading skeleton, so the layout does not jump when data arrives. */
export function Loading({ label = "Loading…", rows = 3 }: LoadingProps) {
  return (
    <div role="status">
      <span className="visually-hidden">{label}</span>
      <div className="placeholder-glow" aria-hidden="true">
        {Array.from({ length: rows }, (_, index) => (
          <span key={index} className={`placeholder d-block mb-2 col-${String(12 - (index % 3) * 2)}`} />
        ))}
      </div>
    </div>
  );
}
