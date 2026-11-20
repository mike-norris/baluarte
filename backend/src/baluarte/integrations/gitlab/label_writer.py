"""The label and issue writer (ADR-0009, ADR-0011), on the shared base in ``writer``.

``LabelWriter`` holds the label write token and is built only by the label plan applier
(``baluarte.labels.plans``) and the issue plan applier (``baluarte.timetracking.issue_plans``).
It can do exactly these things:

- ``POST /groups/:id/labels`` with ``name``, ``color`` and ``description``;
- ``PUT /groups/:id/labels/:label_id`` or ``PUT /projects/:id/labels/:label_id`` with ``new_name``,
  ``color`` and ``description`` (GitLab's project endpoint needs ``new_name`` or ``color``, so
  the colour is always sent);
- ``PUT /projects/:id/issues/:iid`` with only ``add_labels`` or ``remove_labels``, one label;
- ``POST /projects/:id/issues/:iid/time_estimate`` with ``duration``.

There is no delete, and each write is sent once (see ``writer``).
"""

from baluarte.integrations.gitlab.writer import GitLabWriter, WriteResult

__all__ = ["LabelWriter", "WriteResult"]


class LabelWriter(GitLabWriter):
    def create_group_label(
        self, group_id: int, name: str, color: str, description: str | None
    ) -> WriteResult:
        data = {"name": name, "color": color}
        if description is not None:
            data["description"] = description
        return self._send("POST", f"/groups/{group_id}/labels", data)

    def update_label(
        self,
        owner_type: str,
        owner_id: int,
        label_id: int,
        *,
        color: str,
        new_name: str | None = None,
        description: str | None = None,
    ) -> WriteResult:
        if owner_type not in ("group", "project"):
            raise ValueError("labels belong to a group or a project")
        data = {"color": color}
        if new_name is not None:
            data["new_name"] = new_name
        if description is not None:
            data["description"] = description
        return self._send("PUT", f"/{owner_type}s/{owner_id}/labels/{label_id}", data)

    def issue_label(self, project_id: int, iid: int, label: str, *, add: bool) -> WriteResult:
        """Add or remove one label on an issue, leaving its other labels as they are."""
        if not label or "," in label:
            raise ValueError("one label, without commas")
        key = "add_labels" if add else "remove_labels"
        return self._send("PUT", f"/projects/{project_id}/issues/{iid}", {key: label})

    def issue_estimate(self, project_id: int, iid: int, seconds: int) -> WriteResult:
        """Set an issue's time estimate, in whole minutes."""
        if seconds <= 0 or seconds % 60:
            raise ValueError("an estimate is a positive number of whole minutes")
        return self._send(
            "POST",
            f"/projects/{project_id}/issues/{iid}/time_estimate",
            {"duration": f"{seconds // 60}m"},
        )
