import type { ApiError } from "../api/client";
import { secondaryClass } from "./styles";

/** An API failure; retrying is offered unless the session or permissions are the cause. */
export function ErrorMessage({ error, retry }: { error: ApiError; retry: () => void }) {
  return (
    <div role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-900">
      <p>{error.message}</p>
      {error.status !== 401 && error.status !== 403 && (
        <button type="button" onClick={retry} className={`mt-3 ${secondaryClass}`}>
          Try again
        </button>
      )}
    </div>
  );
}
