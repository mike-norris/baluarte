import { useState } from "react";

import { ApiError } from "./client";
import type { Loaded } from "./useLoad";

interface Extra<T> {
  key: string;
  items: T[];
  next: number | null;
}

export interface Paged<T> {
  items: T[];
  next: number | null;
  pending: boolean;
  moreError: ApiError | undefined;
  loadMore: () => Promise<void>;
}

/**
 * The first page from ``useLoad``, with later pages appended by "Load more" for the same key.
 * ``cursor`` reads the page's next cursor (``next_after_id`` or ``next_before_id``).
 */
export function usePaged<T, P extends { items: T[] }>(
  key: string,
  first: Loaded<P>,
  cursor: (page: P) => number | null,
  fetchPage: (next: number) => Promise<P>,
): Paged<T> {
  const [extra, setExtra] = useState<Extra<T> | null>(null);
  const [pending, setPending] = useState(false);
  const [moreError, setMoreError] = useState<{ key: string; error: ApiError } | null>(null);
  const current = extra?.key === key ? extra : null;
  const items = first.data ? [...first.data.items, ...(current?.items ?? [])] : [];
  const next = current ? current.next : first.data ? cursor(first.data) : null;

  async function loadMore() {
    if (next === null) return;
    setPending(true);
    try {
      const page = await fetchPage(next);
      setExtra({ key, items: [...(current?.items ?? []), ...page.items], next: cursor(page) });
      setMoreError(null);
    } catch (error) {
      setMoreError({
        key,
        error:
          error instanceof ApiError ? error : new ApiError(0, "The response could not be read."),
      });
    } finally {
      setPending(false);
    }
  }

  return {
    items,
    next,
    pending,
    moreError: moreError?.key === key ? moreError.error : undefined,
    loadMore,
  };
}
