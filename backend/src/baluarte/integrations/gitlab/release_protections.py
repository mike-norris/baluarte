"""Read each project's release protections, as evidence for release governance.

Runs in the change-evidence job (``gitlab.collect_changes``), after changes are read, with the
per-project reader framework (``baluarte.integrations.gitlab.project_reads``) for every active
project; every request is a GET (docs/RELEASE_GOVERNANCE.md, ADR-0017):

- ``GET /projects/:id/protected_tags``: which tag patterns are protected and who may create them.
- ``GET /projects/:id/protected_environments``, and ``GET /groups/:id/protected_environments`` for
  each group above the project (read once per group and collection): required approvals,
  approval rules and who may deploy. An approval rule is often set on a group. Protected
  environments are GitLab Premium or Ultimate.
- ``GET /projects/:id``: ``allow_pipeline_trigger_approve_deployment``, whether whoever started
  a pipeline (for a tag, its creator) may approve its deployment.

Each project gets one observation with source ``RELEASE_PROTECTION_SOURCE``. Each part carries its
own status (``ok``, ``forbidden``, ``not_found`` or ``failed``); the observation is ``ok`` when
every part was read and ``partial`` otherwise. Access entries are summarised by kind and role
(``baluarte.integrations.gitlab.branches``), never names or IDs.
"""

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import Engine

from baluarte.evidence.observations import Observed
from baluarte.integrations.gitlab.access import _groups
from baluarte.integrations.gitlab.branches import _level
from baluarte.integrations.gitlab.client import (
    Forbidden,
    GitLabClient,
    GitLabError,
    NotFound,
    Unauthorized,
)
from baluarte.integrations.gitlab.project_reads import ProjectTarget, Reader, TokenRejected

RELEASE_PROTECTION_SOURCE = "GET release protections"
PARTS = ("protected_tags", "environments", "group_environments", "pipeline_trigger_approval")


def _ref(target: ProjectTarget) -> str:
    return f"/projects/{target.gitlab_id}"


def _levels(entries: Any) -> list[dict[str, Any]] | None:
    if not isinstance(entries, list):
        return None
    summary = [_level(entry) for entry in entries]
    return sorted(summary, key=lambda item: (item["kind"], item["access_level"] or -1))


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def summarise_tags(items: list[Any]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "name": item["name"],
                "create_access_levels": _levels(item.get("create_access_levels")),
            }
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ),
        key=lambda item: item["name"],
    )


def summarise_environments(items: list[Any]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "name": item["name"],
                "required_approval_count": _int(item.get("required_approval_count")),
                "approval_rules": [
                    {
                        **_level(rule),
                        "required_approvals": _int(rule.get("required_approvals")),
                    }
                    for rule in item.get("approval_rules") or []
                    if isinstance(rule, dict)
                ],
                "deploy_access_levels": _levels(item.get("deploy_access_levels")),
            }
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ),
        key=lambda item: item["name"],
    )


def _status(error: GitLabError) -> str:
    if isinstance(error, Forbidden):
        return "forbidden"
    if isinstance(error, NotFound):
        return "not_found"
    return "failed"


def _read_part(target: ProjectTarget, fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"status": "ok", **fetch()}
    except Unauthorized as error:
        raise TokenRejected(
            Observed(
                target.row_id,
                RELEASE_PROTECTION_SOURCE,
                _ref(target),
                "forbidden",
                error=str(error),
            )
        ) from error
    except GitLabError as error:
        return {"status": _status(error), "error": str(error)}


def _all(client: GitLabClient, path: str) -> list[Any]:
    return [item for page in client.paginate(path) for item in page]


def release_protection_reader(client: GitLabClient, engine: Engine, instance_id: int) -> Reader:
    """A reader over this pass's group inventory, reading each group's environments once."""
    groups = _groups(engine, instance_id)
    group_reads: dict[int, dict[str, Any]] = {}

    def group_environments(target: ProjectTarget) -> dict[str, Any]:
        if target.namespace_kind != "group":
            return {"status": "ok", "groups": []}
        found: list[dict[str, Any]] = []
        failed: dict[str, Any] | None = None
        gitlab_id: int | None = target.namespace_gitlab_id
        seen: set[int] = set()
        while gitlab_id is not None and gitlab_id not in seen:
            seen.add(gitlab_id)
            group = groups.get(gitlab_id)
            if gitlab_id not in group_reads:
                path = f"/groups/{gitlab_id}/protected_environments"

                def fetch(path: str = path) -> dict[str, Any]:
                    return {"items": summarise_environments(_all(client, path))}

                group_reads[gitlab_id] = _read_part(target, fetch)
            read = group_reads[gitlab_id]
            if read["status"] != "ok":
                failed = failed or read
            elif read["items"]:
                found.append(
                    {"path": group["path"] if group else None, "environments": read["items"]}
                )
            gitlab_id = group["parent"] if group else None
        if failed is not None:
            return {"status": failed["status"], "error": failed.get("error"), "groups": found}
        # Nearest group last, as in the two-factor evidence.
        return {"status": "ok", "groups": list(reversed(found))}

    def read(
        client: GitLabClient, target: ProjectTarget, _done: Mapping[str, Observed]
    ) -> Observed:
        base = _ref(target)
        tags = _read_part(
            target, lambda: {"items": summarise_tags(_all(client, f"{base}/protected_tags"))}
        )
        environments = _read_part(
            target,
            lambda: {
                "items": summarise_environments(_all(client, f"{base}/protected_environments"))
            },
        )

        def setting() -> dict[str, Any]:
            body = client.get_json(base)
            value = (
                body.get("allow_pipeline_trigger_approve_deployment")
                if isinstance(body, dict)
                else None
            )
            return {"allowed": value if isinstance(value, bool) else None}

        payload = {
            "protected_tags": tags,
            "environments": environments,
            "group_environments": group_environments(target),
            "pipeline_trigger_approval": _read_part(target, setting),
        }
        complete = all(payload[name]["status"] == "ok" for name in PARTS)
        return Observed(
            target_id=target.row_id,
            source=RELEASE_PROTECTION_SOURCE,
            source_ref=base,
            collection_status="ok" if complete else "partial",
            payload=payload,
        )

    return Reader(
        key="release_protections",
        source=RELEASE_PROTECTION_SOURCE,
        noun="release-protection",
        source_ref=_ref,
        read=read,
        # A part refused for one project (a role, or a GitLab Free project) is an answer.
        stop_on=frozenset({"failed"}),
    )
