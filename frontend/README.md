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
- **Personal projects:** projects in a user namespace carry a "Personal" tag. Projects discovered before namespace kinds were recorded have no tag until the next project discovery.
- **Filters:** search, state, visibility, archived and collection errors are kept in the URL (`?view=groups&state=missing`), so a filtered view can be shared. "Load more" follows `next_after_id`.
- **Errors and retries:** API errors show a retry; a 401 refreshes the session and returns to sign-in.
- **Times:** shown in UTC.

`src/api/` holds the typed API client and the `useLoad` hook, and `src/inventory/` holds the page's helpers.

## Freshness

Signed-in users see one status in the header on every page, as text and colour (for example "GitLab data: Stale"): the worst of two signals, GitLab collection and policy evaluation. `src/freshness/` is the one place for freshness statuses, labels, colours and severity order.

- **When it checks:** the header calls `GET /api/v1/gitlab/freshness` and `GET /api/v1/evaluations/freshness` on sign-in, on every navigation and every minute while a page stays open. The previous status stays visible while a check is in flight; checks are aborted on navigation and unmount.
- **Worst wins:** a failed evaluation shows "Failed" even when collection is fresh. The badge links to the details of the signal that set it: `/inventory` for collection (also when both are equally bad), the dashboard for evaluation. An evaluation that never ran reads "Never evaluated".
- **Unknown is not current:** if either check fails, the header shows "Status unknown" and replaces any earlier "Up to date". Unknown ranks between failed and never, so it is never treated as compliant.
- **Announcements:** failed, stale and unknown are announced once through a polite live region, naming collection or evaluation. Re-checks that return the same status, and navigation, do not repeat the announcement.
- **Results behind collection:** `behind_collection` (a project discovery finished after the latest successful evaluation started) does not change the badge, because a queued evaluation follows every discovery and the badge would otherwise flash. The dashboard and the Findings and Evidence placeholders say that results may not reflect the latest collection.
- **Placeholders:** Findings and Evidence show both statuses, the oldest successful collection across projects, groups and capabilities, and the last successful evaluation. They show no time when any part has never succeeded or the status is unknown.

## Dashboard

`/` shows compliance posture from the evaluation API (`src/api/evaluations.ts`):

- **Evaluation status:** the evaluation freshness, with failed and stale announced as alerts, whether a run is running or queued, results behind collection, and the last run's error.
- **Rule results:** totals by outcome from the latest *successful* run in `GET /api/v1/evaluations/runs` (its `counts.outcomes`). If a newer run has not succeeded, the page says the totals come from an earlier run. The pass share is "X of N rule results", where N includes `unknown` and `unsupported`. Neither is ever counted as passing; each outcome is shown with its meaning. The share is floored, so it never reads 100% unless every result passed.
- **Open findings:** counted by severity and by policy and rule from `GET /api/v1/findings?state=open`, following `next_after_id` for at most 5 pages of 200. When more remain, every count reads "at least". Open findings whose latest result is unknown or unsupported are called out, since they could not be re-checked. "No open findings" says that this is not compliance, because unknown results never open a finding.
- **Links:** counts link to `/findings` with the API's own filter names (`state`, `outcome`, `severity`, `policy`, `rule`, `project`), for the Findings page (BAL-024) to read.
