"""The template writer (roadmap M14.4), on the shared base in ``writer``.

``TemplateWriter`` holds the template write token, separate from the read token and from the
label and remediation write tokens, and is built only by the change set applier
(``baluarte.templates.execute``). It can make exactly the changes a template setting names:

- ``PUT /projects/:id`` with only ``visibility``, or only ``topics`` (tags synchronized to
  topics, M14.5);
- ``POST /projects/:id/protected_branches`` for one branch by name, with the access levels given
  (Maintainer for a new protection), and ``DELETE`` of one protection by its exact name;
- ``POST /projects/:id/approvals`` with one approval setting;
- ``POST``, ``PUT`` and ``DELETE`` of one approval rule's required approvals.

Each call is one request; retries are decided by the applier.
"""

from urllib.parse import quote

from baluarte.integrations.gitlab.writer import GitLabWriter, WriteResult

__all__ = ["APPROVAL_SETTINGS", "MAINTAINER", "TemplateWriter", "WriteResult"]

MAINTAINER = 40
VISIBILITIES = ("private", "internal", "public")
# The project approval settings a template sets, by GitLab name.
APPROVAL_SETTINGS = (
    "merge_requests_author_approval",
    "merge_requests_disable_committers_approval",
    "reset_approvals_on_push",
    "disable_overriding_approvers_per_merge_request",
)
RULE_NAME = "Baluarte approvals"


def _branch(branch: str) -> str:
    if not branch or "*" in branch:
        raise ValueError("one branch by name, without wildcards")
    return quote(branch, safe="")


class TemplateWriter(GitLabWriter):
    def set_visibility(self, project_id: int, visibility: str) -> WriteResult:
        if visibility not in VISIBILITIES:
            raise ValueError("visibility is private, internal or public")
        return self._send("PUT", f"/projects/{project_id}", {"visibility": visibility})

    def set_topics(self, project_id: int, topics: list[str]) -> WriteResult:
        if not all(isinstance(topic, str) and topic.strip() for topic in topics):
            raise ValueError("topics are non-empty strings")
        return self._send("PUT", f"/projects/{project_id}", {"topics": list(topics)}, as_json=True)

    def protect_branch(
        self, project_id: int, branch: str, levels: dict[str, int] | None = None
    ) -> WriteResult:
        _branch(branch)
        chosen = {
            "push_access_level": MAINTAINER,
            "merge_access_level": MAINTAINER,
            "unprotect_access_level": MAINTAINER,
            **(levels or {}),
        }
        return self._send(
            "POST", f"/projects/{project_id}/protected_branches", {"name": branch, **chosen}
        )

    def unprotect_branch(self, project_id: int, branch: str) -> WriteResult:
        return self._send(
            "DELETE", f"/projects/{project_id}/protected_branches/{_branch(branch)}", {}
        )

    def set_approval_setting(self, project_id: int, setting: str, value: bool) -> WriteResult:
        if setting not in APPROVAL_SETTINGS:
            raise ValueError(f"{setting} is not a template approval setting")
        return self._send(
            "POST", f"/projects/{project_id}/approvals", {setting: "true" if value else "false"}
        )

    def create_approval_rule(self, project_id: int, approvals: int) -> WriteResult:
        return self._send(
            "POST",
            f"/projects/{project_id}/approval_rules",
            {"name": RULE_NAME, "approvals_required": approvals, "rule_type": "any_approver"},
        )

    def update_approval_rule(self, project_id: int, rule_id: int, approvals: int) -> WriteResult:
        return self._send(
            "PUT",
            f"/projects/{project_id}/approval_rules/{rule_id}",
            {"approvals_required": approvals},
        )

    def delete_approval_rule(self, project_id: int, rule_id: int) -> WriteResult:
        return self._send("DELETE", f"/projects/{project_id}/approval_rules/{rule_id}", {})
