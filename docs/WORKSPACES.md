# Workspaces and connections

Roadmap milestone M11 ([ROADMAP.md](ROADMAP.md#m11--workspaces-and-connections)), decided in [ADR-0013](architecture/ADR-0013-workspaces-and-connections.md). A **workspace** is the isolation boundary: an organization's connections and everything built on them. A **connection** is one GitLab or GitHub deployment with its own credentials.

## Status

| Step | What it delivers | State |
|---|---|---|
| M11.1 Foundation | The `workspace` table and a `default` workspace; `gitlab_instance` rows become connections (`workspace_id`, `provider`, `name`, `paused`); the API's routers find their connection through `baluarte.workspaces.current_connection` | Done |
| M11.2 Workspace ownership | Baselines, assignments and changes, the control mapping's changes, exclusions, finding exceptions, remediation, label and issue plans, taxonomies, tags, manager scopes, exports and notifications carry `workspace_id` (migration `0030`); a baseline's name and version, its proposed version, a taxonomy's versions and a tag's key and value are unique per workspace | Done |
| M11.3 Connections | Credentials per connection (sealed); a Connections page to add, test, pause and remove a connection; health, coverage and errors per connection; URL checks against server-side request forgery; collection jobs run per connection | Planned |
| M11.4 Several workspaces | Roles per workspace from identity provider groups; a workspace switcher; every API query, job, export and notification scoped to the caller's workspace, and audit events recording it; isolation tests on every router and job | Planned |

Each step is deployable on its own and changes nothing an existing install relies on.

## What belongs where

| Level | Holds |
|---|---|
| Deployment | Users and sessions, the identity provider, Baluarte's own operating profile, attestations and compliance exceptions, backups, audit log shipping, settings secrets |
| Workspace | Connections; baselines and assignments; the control mapping and its changes; monitoring exclusions; finding exceptions; tags; label taxonomies; remediation, label and issue plans; manager scopes; notifications; exports; from later milestones, the issue register, templates, time and finance |
| Connection | Inventory (groups, projects, repositories), capability detection, collection runs and cursors, observations, evaluations and findings, change records, labels, time records |

Since M11.2, workspace-owned tables carry their own `workspace_id`. Rows created with a connection take that connection's workspace (`baluarte.workspaces.workspace_of`); the rest take the current workspace (`current_workspace_id`), which is the default workspace until M11.4. Some of those tables still carry `instance_id` as well; M11.3 decides, table by table, whether it stays as the connection a record is about.

## Upgrade guarantees

- Migrations add tables and columns and back-fill them. They never rewrite observations, evaluations or audit events, so every stored digest still verifies.
- Existing data lands in the `default` workspace, and an install with one GitLab connection behaves exactly as before.
- Each step's migration has a downgrade that keeps existing rows.

## Limits

- Until M11.3, a workspace has one connection: the GitLab configured in the deployment or on the Settings page.
- Until M11.4, a deployment has one workspace.
- The connection table keeps its name, `gitlab_instance`, until GitHub connections arrive in M13.
