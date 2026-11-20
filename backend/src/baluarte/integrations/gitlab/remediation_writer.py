"""The remediation writer (ADR-0012), on the shared base in ``writer``.

``RemediationWriter`` holds the remediation write token, separate from the read token and from
the label write token, and is built only by the remediation plan applier
(``baluarte.remediation.execute``). It can do exactly the two allowlisted actions
(docs/REMEDIATION.md), with the values fixed here rather than taken from the caller:

- ``PUT /projects/:id`` with only ``visibility``, and only ``private`` or ``internal``: it can
  restrict a project, never make one public;
- ``POST /projects/:id/protected_branches`` with ``name`` and push, merge and unprotect access
  all at Maintainer (40), GitLab's default for a new protection.

There is no delete, no unprotect and no change to an existing protection, and each write is sent
once (see ``writer``).
"""

from baluarte.integrations.gitlab.writer import GitLabWriter, WriteResult

__all__ = ["MAINTAINER", "RESTRICTED", "RemediationWriter", "WriteResult"]

# The visibility levels a project may be restricted to.
RESTRICTED = ("private", "internal")
# GitLab's access level for Maintainer.
MAINTAINER = 40


class RemediationWriter(GitLabWriter):
    def restrict_visibility(self, project_id: int, visibility: str) -> WriteResult:
        if visibility not in RESTRICTED:
            raise ValueError("a project is only ever restricted, to private or internal")
        return self._send("PUT", f"/projects/{project_id}", {"visibility": visibility})

    def protect_branch(self, project_id: int, branch: str) -> WriteResult:
        if not branch or "*" in branch:
            raise ValueError("one branch by name, without wildcards")
        return self._send(
            "POST",
            f"/projects/{project_id}/protected_branches",
            {
                "name": branch,
                "push_access_level": MAINTAINER,
                "merge_access_level": MAINTAINER,
                "unprotect_access_level": MAINTAINER,
            },
        )
