# Workspaces and connections

Roadmap milestone M11 ([ROADMAP.md](ROADMAP.md#m11--workspaces-and-connections)), decided in [ADR-0013](architecture/ADR-0013-workspaces-and-connections.md). A **workspace** is the isolation boundary: an organization's connections and everything built on them. A **connection** is one GitLab or GitHub deployment with its own credentials.

## Status

| Step | What it delivers | State |
|---|---|---|
| M11.1 Foundation | The `workspace` table and a `default` workspace; `gitlab_instance` rows become connections (`workspace_id`, `provider`, `name`, `paused`); the API's routers find their connection through `baluarte.workspaces.current_connection` | Done |
| M11.2 Workspace ownership | Baselines, assignments, the control mapping, tags, taxonomies, exclusions, plans, exceptions and exports belong to a workspace, with unique constraints scoped to it; audit events record their workspace | Next |
| M11.3 Connections | Credentials per connection (sealed); a Connections page to add, test, pause and remove a connection; health, coverage and errors per connection; URL checks against server-side request forgery; collection jobs run per connection | Planned |
| M11.4 Several workspaces | Roles per workspace from identity provider groups; a workspace switcher; every API query, job, export and notification scoped to the caller's workspace; isolation tests on every router and job | Planned |

Each step is deployable on its own and changes nothing an existing install relies on.

## What belongs where

| Level | Holds |
|---|---|
| Deployment | Users and sessions, the identity provider, Baluarte's own operating profile, attestations and compliance exceptions, backups, audit log shipping, settings secrets |
| Workspace | Connections; baselines and assignments; the control mapping and its changes; monitoring exclusions; finding exceptions; tags; label taxonomies; remediation, label and issue plans; manager scopes; notifications; exports; from later milestones, the issue register, templates, time and finance |
| Connection | Inventory (groups, projects, repositories), capability detection, collection runs and cursors, observations, evaluations and findings, change records, labels, time records |

Today `gitlab_instance` (the connection) is the key that workspace-owned tables carry as `instance_id`. M11.2 gives those tables a `workspace_id` of their own, so a workspace can hold several connections.

## Upgrade guarantees

- Migrations add tables and columns and back-fill them. They never rewrite observations, evaluations or audit events, so every stored digest still verifies.
- Existing data lands in the `default` workspace, and an install with one GitLab connection behaves exactly as before.
- Each step's migration has a downgrade that keeps existing rows.

## Limits

- Until M11.3, a workspace has one connection: the GitLab configured in the deployment or on the Settings page.
- Until M11.4, a deployment has one workspace.
- The connection table keeps its name, `gitlab_instance`, until GitHub connections arrive in M13.
