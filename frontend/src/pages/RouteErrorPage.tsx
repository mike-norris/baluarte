import { useRouteError } from "react-router";

import { isChunkLoadError } from "../chunkReload";

/** Shown instead of React Router's default when a page fails to load or render. */
export function RouteErrorPage() {
  const error = useRouteError();
  const stale = isChunkLoadError(error);
  return (
    <main className="page page-center">
      <div className="container container-tight py-4">
        <section role="alert" aria-labelledby="error-title" className="card card-md">
          <div className="card-status-top bg-danger" />
          <div className="card-body">
            <h1 id="error-title" className="h2">
              {stale ? "Baluarte has been updated" : "Something went wrong"}
            </h1>
            <p className="mt-2">
              {stale
                ? "This page could not be loaded because a newer version of Baluarte is running. Reload to continue."
                : "This page could not be shown. Reload to try again; if it keeps happening, tell your administrator."}
            </p>
            <div className="mt-3 d-flex gap-2">
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="btn btn-primary"
              >
                Reload
              </button>
              <a href="/" className="btn">
                Go to the dashboard
              </a>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
