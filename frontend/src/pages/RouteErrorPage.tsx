import { useRouteError } from "react-router";

import { secondaryClass } from "../components/styles";
import { isChunkLoadError } from "../chunkReload";

/** Shown instead of React Router's default when a page fails to load or render. */
export function RouteErrorPage() {
  const error = useRouteError();
  const stale = isChunkLoadError(error);
  return (
    <main className="mx-auto max-w-2xl p-6">
      <section
        role="alert"
        aria-labelledby="error-title"
        className="rounded border border-red-200 bg-red-50 p-4 text-red-900"
      >
        <h1 id="error-title" className="text-lg font-semibold">
          {stale ? "Baluarte has been updated" : "Something went wrong"}
        </h1>
        <p className="mt-2 text-sm">
          {stale
            ? "This page could not be loaded because a newer version of Baluarte is running. Reload to continue."
            : "This page could not be shown. Reload to try again; if it keeps happening, tell your administrator."}
        </p>
        <div className="mt-3 flex gap-2">
          <button type="button" onClick={() => window.location.reload()} className={secondaryClass}>
            Reload
          </button>
          <a href="/" className={secondaryClass}>
            Go to the dashboard
          </a>
        </div>
      </section>
    </main>
  );
}
