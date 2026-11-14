interface PaginationProps {
  /** For example "Showing 1–50". */
  range?: string;
  hasPrevious: boolean;
  hasNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
}

/** Previous and next for the API's cursor-paged lists. */
export function Pagination({ range, hasPrevious, hasNext, onPrevious, onNext }: PaginationProps) {
  if (!hasPrevious && !hasNext && !range) return null;
  return (
    <div className="card-footer d-flex flex-wrap align-items-center gap-2">
      {range && <p className="m-0 text-secondary">{range}</p>}
      <div className="btn-list ms-auto">
        <button type="button" className="btn" disabled={!hasPrevious} onClick={onPrevious}>
          Previous
        </button>
        <button type="button" className="btn" disabled={!hasNext} onClick={onNext}>
          Next
        </button>
      </div>
    </div>
  );
}
