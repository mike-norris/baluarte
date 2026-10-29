# Baluarte web

React, TypeScript (strict) and Vite single-page app. Requires Node.js 22+.

## Development

```sh
npm ci
npm run dev        # http://localhost:5173, proxies /api to the backend on 127.0.0.1:8000
npm test
npm run lint
npm run typecheck
npm run build      # static assets in dist/
```

## Sign-in

The app asks `GET /api/v1/auth/me` for the session on load and shows sections only to signed-in users. Sign-in is a full-page navigation to `/api/v1/auth/login`; the API handles OIDC and sets an `HttpOnly` session cookie that the app never reads. Sign-out sends `POST /api/v1/auth/logout` with the `X-CSRF-Token` header from `/auth/me`. Failed sign-ins land on `/sign-in?error=<code>`, which shows a fixed message for each code.

Hiding sections in the browser is a convenience only: the API enforces every permission. To sign in through the Vite dev server, set the backend's `BALUARTE_PUBLIC_URL=http://localhost:5173` and register `http://localhost:5173/api/v1/auth/callback` with your identity provider.

## Inventory

`/inventory` lists GitLab projects and groups from `GET /api/v1/gitlab/projects` and `/groups`, with the collection status from `GET /api/v1/gitlab/freshness` above them:

- **Collection status:** a failed or stale collection is announced as an alert before any data, so an old success never reads as current.
- **Unknown values:** these are shown as such: an unknown visibility is "Unknown", an empty repository has no default branch, and `missing` is explained as "deleted, or hidden from the token".
- **Filters:** search, state, visibility, archived and collection errors are kept in the URL (`?view=groups&state=missing`), so a filtered view can be shared. "Load more" follows `next_after_id`.
- **Errors and retries:** API errors show a retry; a 401 refreshes the session and returns to sign-in.
- **Times:** shown in UTC.

`src/api/` holds the typed API client, and `src/inventory/` holds the page's helpers.
