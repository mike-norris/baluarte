# Tags

GitLab groups follow how code is organized, which is not always how an organization reports. Tags are `key=value` pairs kept in Baluarte, such as `team=payments` or `tier=1`, that group projects across the GitLab hierarchy. The inventory, findings, time summaries, the manager overview and trends can all be filtered or grouped by tag.

Implemented in `backend/src/baluarte/tags.py` (migration `0023`) and `backend/src/baluarte/api/v1/tags.py`.

Tags never change GitLab, and they are not evidence of compliance: they only group results.

## Keys and values

Keys and values are lowercase: a letter or digit, then up to 62 letters, digits, `.`, `_` or `-` (`[a-z0-9][a-z0-9._-]{0,62}`). The database checks the same pattern. A tag also has an optional description (up to 500 characters) and colour (`#RRGGBB`, stored in upper case). Key and value cannot change after creation: create another tag instead.

## How tags apply

A tag is assigned directly to groups and projects.

| Assigned to | Applies to |
|---|---|
| A group | The group, its subgroups at any depth, and their projects. |
| A project | That project. |

A project has at most one value per key. When several assignments of a key reach it, the closest wins:

1. a value assigned to the project directly;
2. otherwise the value of the nearest group above it;
3. a group further up only when no nearer group has that key.

Two values of the same key cannot be assigned directly to the same group or project: the second is refused with `409 key_conflict`, listing the paths. Inheritance follows the inventory's group tree (`parent_gitlab_id`), so a project moved in GitLab picks up its new groups' tags at the next discovery.

`tags.index(db, instance_id)` resolves every open assignment once. Every consumer uses it (`TagIndex.of_project`, `of_group`, `projects_with`, `groups_with`), so there is one rule for which tags apply.

## Where tags show

| Area | Effect |
|---|---|
| Inventory | `GET /gitlab/projects`, `GET /gitlab/groups` and their detail reads return `tags: [{key, value, via, from_path}]`. `via` is `project` for a direct assignment and `group` for an inherited one; `from_path` is where it comes from. |
| Filters | The inventory lists and `GET /findings` accept a repeatable `tag=key:value`; every one must apply (AND). An invalid filter is `422 invalid_tag_filter`. |
| Time | `GET /time/summary?by=tag:<key>` sums time by value; `tag=key:value` filters. |
| Manager space | `GET /manager/overview?by_tag=<key>` has a row per value, and `tag=key:value` limits the overview and trends. |
| Evidence export | `inventory.csv` has a `tags` column, `key=value` pairs separated by `;` (export format v5). |

Tags have no history in the inventory sense: views as of a past date use today's tags, and say so (`current_only`).

## API

Reading needs `inventory:read`; changes need `tags:write` (administrators, analysts and managers).

| Method and path | Purpose |
|---|---|
| `GET /tags` | Every tag with how many groups and projects it is assigned to directly. |
| `POST /tags` | Create `{key, value, description?, color?}`. `422 invalid_name`, `409 tag_exists`. |
| `GET /tags/{id}` | The tag and its open assignments. |
| `PATCH /tags/{id}` | Change `description` or `color`; only the fields sent change, `null` clears. |
| `POST /tags/{id}/assign` | `{targets: [{type: "group"|"project", gitlab_id}]}`, at most 500. Targets that already have the tag are left alone. `409 unknown_target`, `409 key_conflict`. |
| `POST /tags/{id}/unassign` | Same body; targets without the tag are ignored. |

```json
{
  "id": 3,
  "key": "team",
  "value": "payments",
  "description": "Payments squads",
  "color": "#1F75CB",
  "created_by": {"actor": "user:4", "user_id": 4, "display_name": "Ana"},
  "created_at": "2026-11-06T10:00:00Z",
  "groups": 1,
  "projects": 0,
  "assignments": [
    {"id": 9, "target_type": "group", "target_gitlab_id": 11, "target_path": "acme/team",
     "assigned_by": {"actor": "user:4", "user_id": 4, "display_name": "Ana"},
     "assigned_at": "2026-11-06T10:01:00Z"}
  ]
}
```

## Integrity

`tag` rows keep their key and value: a trigger allows only `description`, `color` and `updated_at` to change and refuses `DELETE`. `tag_assignment` rows are ended rather than deleted (`ended_at`, `ended_by`), so the history stays; a partial unique index allows one open assignment per tag and target. Every change is audited:

| Event | Details |
|---|---|
| `tags.created` | `key`, `value` |
| `tags.updated` | `description`, `color` |
| `tags.assigned`, `tags.unassigned` | `tag` (`key=value`), `targets` (`group:<path>` or `project:<path>`) |
