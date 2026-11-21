# Workspaces and connections

Roadmap milestone M11 ([ROADMAP.md](ROADMAP.md#m11--workspaces-and-connections)), decided in [ADR-0013](architecture/ADR-0013-workspaces-and-connections.md). A **workspace** is the isolation boundary: an organization's connections and everything built on them. A **connection** is one GitLab or GitHub deployment with its own credentials.

## Status

| Step | What it delivers | State |
|---|---|---|
| M11.1 Foundation | The `workspace` table and a `default` workspace; `gitlab_instance` rows become connections (`workspace_id`, `provider`, `name`, `paused`); the API's routers find their connection through `baluarte.workspaces.current_connection` | Done |
| M11.2 Workspace ownership | Baselines, assignments and changes, the control mapping's changes, exclusions, finding exceptions, remediation, label and issue plans, taxonomies, tags, manager scopes, exports and notifications carry `workspace_id` (migration `0030`); a baseline's name and version, its proposed version, a taxonomy's versions and a tag's key and value are unique per workspace | Done |
| M11.3 Connections | A token per connection, sealed as `connection_token:<id>`; a Connections page (`/connections`, `/api/v1/connections`) to add, test, collect, pause and remove a connection; health and the latest error per connection; URL checks against server-side request forgery; collection jobs and their schedule run per connection (migration `0031`) | Done |
| M11.4 Several workspaces | Roles per workspace from identity provider groups; a Workspaces page (`/workspaces`, `/api/v1/workspaces`); a workspace and connection switcher, so every page works on any connection; every API query scoped to the request's workspace, exports included; audit events record their workspace; deployment settings managed from the default workspace only; isolation tests (migration `0032`) | Done |

Each step is deployable on its own and changes nothing an existing install relies on.

## What belongs where

| Level | Holds |
|---|---|
| Deployment | Users and sessions, the identity provider, Baluarte's own operating profile, attestations and compliance exceptions, backups, audit log shipping, settings secrets |
| Workspace | Connections; baselines and assignments; the control mapping and its changes; monitoring exclusions; finding exceptions; tags; label taxonomies; remediation, label and issue plans; manager scopes; notifications; exports; from later milestones, the issue register, templates, time and finance |
| Connection | Inventory (groups, projects, repositories), capability detection, collection runs and cursors, observations, evaluations and findings, change records, labels, time records |

Since M11.2, workspace-owned tables carry their own `workspace_id`. Rows created with a connection take that connection's workspace (`baluarte.workspaces.workspace_of`); the rest take the current workspace (`current_workspace_id`), which is the request's workspace (the default workspace for jobs and the CLI). Tables that also carry `instance_id` keep it as the connection a record is about: an exclusion, an exception or a plan is for one connection's projects, inside its workspace.

## Upgrade guarantees

- Migrations add tables and columns and back-fill them. They never rewrite observations, evaluations or audit events, so every stored digest still verifies.
- Existing data lands in the `default` workspace, and an install with one GitLab connection behaves exactly as before.
- Each step's migration has a downgrade that keeps existing rows.

## Connections

- **The primary connection** is the GitLab set in the deployment or on the Settings page, as before. Its URL, token and schedule stay there. On the Connections page it can be tested, collected now or paused.
- **Added connections** are created on the Connections page. Each has its own name, URL, read-only token, timeout and schedule. The token is sealed with the deployment's secret key (so `BALUARTE_SECRET_KEY_FILE` is required) and never returned by the API.
- **Address checks.** An added connection's host must resolve only to public addresses, unless an administrator allows a private network for it (a self-managed GitLab). Loopback, link-local (including cloud metadata), multicast, unspecified and reserved addresses are always refused. The check runs when a connection is added or changed and again before every job, so a name that later resolves elsewhere is refused. Redirects are never followed.
- **Jobs.** Every collection job names its connection in its payload (`connection_id`); follow-up jobs (evaluation, security policies, changes, labels, time) carry it on. Jobs without one collect the primary connection, as before. Each added connection is scheduled on its own interval.
- **Pause and remove.** A paused connection is not scheduled. Removing a connection also forgets its token. Neither deletes inventory, findings or evidence, and adding a removed URL again restores the connection with its data.
- **Audit.** Adding, restoring, changing, testing, collecting and removing a connection are audit events (`connection.*`); token values are never recorded.

## Several workspaces

- **Roles per workspace.** Each workspace maps identity provider groups to roles (`workspace.group_roles`). The default workspace uses the sign-in settings' groups. At sign-in, a session records the roles it holds in each workspace (`user_session.workspace_roles`); a person needs a role in at least one. Group changes apply from each person's next sign-in.
- **Choosing.** A request names its workspace with `X-Baluarte-Workspace: <slug>` and, optionally, its connection with `X-Baluarte-Connection: <id>`. Without them, the API uses the person's default workspace (the `default` one when they have a role there) and its primary connection, or its oldest one. Naming a workspace without a role there is refused (403); naming a connection outside the workspace is refused (404). The web app keeps the choice in the browser and offers a switcher at the top of each page when there is more than one choice.
- **Isolation.** `baluarte.auth.dependencies` records the workspace and connection on the request's database session, and an ORM hook (`baluarte.workspaces`) adds the workspace to every query on that session: tables with `workspace_id` directly, tables with `instance_id` through their connection, and findings, tag assignments, notification deliveries and jobs through what they belong to. A record from another workspace is not found (404), not refused, so its existence is not disclosed. Exports and audit packages open their own sessions in the request's workspace. Inserts name their workspace explicitly.
- **Audit.** Every audit event records the workspace it happened in (`audit_event.workspace_id`). Sign-ins, deployment settings and events from before migration `0032` have none and belong to the default workspace's audit log. The digest covers `details` only, so no existing digest changes.
- **Deployment settings.** The identity provider, the primary GitLab, write tokens, review, change evidence and time settings, notification channels, audit shipping and Baluarte's own compliance are shared by every workspace. They are read and changed only while acting in the default workspace, by someone with `settings:manage` there. Administrators of other workspaces manage their own connections.
- **Managing workspaces.** Administrators of the default workspace create workspaces and change their names and groups on the Workspaces page. The `default` workspace cannot be removed, and its groups are the sign-in settings'.

## Limits

- Jobs started by the schedule and the CLI act outside any request. They work on the connection they name and record what they create in that connection's workspace; notifications they raise for a connection (new findings) carry its workspace, and the others (collection health, review reminders, the weekly digest) belong to the default workspace and go to the deployment's channels.
- Workspaces are not deleted. A workspace with no groups grants no roles.
- The connection table keeps its name, `gitlab_instance`, until GitHub connections arrive in M13.
