import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router";

import { AsOfContext, type AsOfState } from "./context";
import { AS_OF_PARAM, asOfAware, parseAsOf } from "./format";

/**
 * Keeps the past-date view across navigation. The URL is the record: a page that can show the
 * past and is opened without ``as_of`` while a date is chosen gets it added, so links inside
 * those pages need not carry it. A date in the URL always wins over the one kept.
 */
export function AsOfProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const fromUrl = parseAsOf(new URLSearchParams(location.search).get(AS_OF_PARAM));
  const [kept, setKept] = useState<string | null>(fromUrl);
  // Follow the URL when it changes, not on every render: while "Back to now" is on its way the
  // old URL is still current, and must not bring the date back.
  const [seen, setSeen] = useState<string | null>(fromUrl);
  if (fromUrl !== seen) {
    setSeen(fromUrl);
    if (fromUrl !== null) setKept(fromUrl);
  }
  const chosen = fromUrl ?? kept;
  const aware = asOfAware(location.pathname);

  useEffect(() => {
    if (!aware || fromUrl !== null || kept === null) return;
    const params = new URLSearchParams(location.search);
    params.set(AS_OF_PARAM, kept);
    void navigate(
      {
        pathname: location.pathname,
        search: `?${params.toString()}`,
        hash: location.hash,
      },
      { replace: true },
    );
  }, [aware, fromUrl, kept, location.pathname, location.search, location.hash, navigate]);

  const choose = useCallback(
    (next: string | null) => {
      setKept(next);
      const params = new URLSearchParams(location.search);
      if (next === null) params.delete(AS_OF_PARAM);
      else if (aware) params.set(AS_OF_PARAM, next);
      const search = params.toString();
      void navigate({
        pathname: location.pathname,
        search: search ? `?${search}` : "",
        hash: location.hash,
      });
    },
    [aware, location.pathname, location.search, location.hash, navigate],
  );

  const value = useMemo<AsOfState>(
    () => ({ asOf: aware ? chosen : null, chosen, choose }),
    [aware, chosen, choose],
  );
  return <AsOfContext value={value}>{children}</AsOfContext>;
}
