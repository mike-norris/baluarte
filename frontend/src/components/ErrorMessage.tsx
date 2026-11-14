import { IconAlertCircle } from "@tabler/icons-react";

import type { ApiError } from "../api/client";

/** An API failure; retrying is offered unless the session or permissions are the cause. */
export function ErrorMessage({ error, retry }: { error: ApiError; retry: () => void }) {
  return (
    <div role="alert" className="alert alert-danger">
      <div className="d-flex gap-2">
        <IconAlertCircle size={20} className="alert-icon flex-shrink-0" aria-hidden="true" />
        <div>
          <p className="mb-0 text-body">{error.message}</p>
          {error.status !== 401 && error.status !== 403 && (
            <button type="button" onClick={retry} className="btn btn-sm mt-2">
              Try again
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
