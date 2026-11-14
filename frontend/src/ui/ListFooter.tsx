import type { ApiError } from "../api/client";
import { ErrorMessage } from "../components/ErrorMessage";

interface ListFooterProps {
  /** "findings", "projects"… */
  noun: string;
  shown: number;
  hasMore: boolean;
  pending: boolean;
  error: ApiError | null | undefined;
  onMore: () => void;
  /** Defaults to "Load more {noun}"; newest-first lists say "Load older …". */
  moreLabel?: string;
}

/** The count and Load more under a card table; cursor-paged lists append. */
export function ListFooter({
  noun,
  shown,
  hasMore,
  pending,
  error,
  onMore,
  moreLabel,
}: ListFooterProps) {
  return (
    <>
      <div className="card-footer d-flex flex-wrap align-items-center gap-2">
        <p className="text-secondary mb-0 me-auto" aria-live="polite">
          {`Showing ${String(shown)} ${noun}${hasMore ? " (more available)" : ""}.`}
        </p>
        {hasMore && (
          <button type="button" onClick={onMore} disabled={pending} className="btn">
            {pending ? "Loading…" : (moreLabel ?? `Load more ${noun}`)}
          </button>
        )}
      </div>
      {error && (
        <div className="card-body">
          <ErrorMessage error={error} retry={onMore} />
        </div>
      )}
    </>
  );
}
