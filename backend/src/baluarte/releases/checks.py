"""Judging one production release against its practice (RG.3, docs/RELEASE_GOVERNANCE.md).

Pure functions over a deployment's observation (``baluarte.integrations.gitlab.changes``) and the
project's release protections (``baluarte.integrations.gitlab.release_protections``), used by the
``gitlab.release.follows_practice`` rule. People are as GitLab records them:

- whoever **started** the release is the user of the pipeline it ran in (for a tag, the tag's
  creator);
- the **approvers** are the deployment's approvals with status ``approved``;
- the **deployer** is the deployment's user.

Each check gives pass, fail or unknown; a check that cannot be read is unknown, never a pass.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from baluarte.changes import assess
from baluarte.changes.assess import Judged, Verdict
from baluarte.integrations.gitlab.branches import matches
from baluarte.integrations.gitlab.changes import parse_time
from baluarte.releases.practices import PracticeSettings, expected, kind_of

KIND_WORDS = {"tag": "the tag", "branch": "the branch", "default_branch": "the default branch"}


@dataclass(frozen=True)
class Release:
    verdict: Verdict
    reasons: list[str]
    # Who started, approved and deployed it, as GitLab records them; None when not known.
    started_by: str | None
    approvers: list[str] | None
    deployer: str | None


def protected_tags(protections: Mapping[str, Any] | None) -> list[str] | None:
    """The project's protected tag patterns, or None when they were not read."""
    part = (protections or {}).get("protected_tags")
    if not isinstance(part, dict) or part.get("status") != "ok":
        return None
    items = part.get("items")
    if not isinstance(items, list):
        return None
    return [item["name"] for item in items if isinstance(item, dict) and "name" in item]


def _part(payload: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    part = payload.get(name)
    return part if isinstance(part, dict) else None


def _approvals(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The approvals with status ``approved``, or None with why they are not known."""
    part = _part(payload, "deployment_approvals")
    if part is None:
        return None, "its approvals were not read"
    if part.get("status") == "not_supported":
        return None, "GitLab did not report its approvals (Premium or Ultimate)"
    if part.get("status") != "ok" or not isinstance(part.get("approvals"), list):
        return None, f"its approvals could not be read ({part.get('status')})"
    approved = [
        a for a in part["approvals"] if isinstance(a, dict) and a.get("status") == "approved"
    ]
    if any(not isinstance(a.get("username"), str) for a in approved):
        return None, "an approver is not known"
    return approved, None


def _started_by(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    part = _part(payload, "trigger")
    if part is None:
        return None, "who started it was not read"
    if part.get("status") != "ok":
        return None, f"who started it could not be read ({part.get('status')})"
    user = part.get("user")
    if not isinstance(user, str):
        return None, "GitLab did not record who started it"
    return user, None


def judge(
    payload: Mapping[str, Any],
    settings: PracticeSettings,
    protections: Mapping[str, Any] | None,
    changes: Mapping[str, Mapping[str, Any]],
    default_branch: str | None,
) -> Release:
    """One production deployment against the practice."""
    name = assess.label(payload)
    ref = payload.get("ref") if isinstance(payload.get("ref"), str) else None
    tag = payload.get("tag") if isinstance(payload.get("tag"), bool) else None
    checks: list[Judged] = []

    # Where it came from.
    kind = kind_of(ref, tag, default_branch)
    wanted = expected(settings, kind, ref)
    if wanted is None:
        checks.append(("unknown", f"whether {name} deployed a tag or a branch is not known"))
    elif not wanted:
        checks.append(
            (
                "fail",
                f"{name} deployed {KIND_WORDS[kind]} {ref}, which the practice does not expect",
            )
        )
    else:
        checks.append(("pass", f"{name} deployed {KIND_WORDS[kind]} {ref}"))
        if settings.production_from == "tag":
            patterns = protected_tags(protections)
            if patterns is None:
                checks.append(("unknown", "the project's protected tags were not read"))
            elif not any(matches(pattern, ref or "") for pattern in patterns):
                checks.append(("fail", f"the tag {ref} of {name} is not protected"))
            else:
                checks.append(("pass", f"the tag {ref} is protected"))

    # Approvals and people.
    approved, why_not = _approvals(payload)
    approvers = sorted({a["username"] for a in approved}) if approved is not None else None
    started_by, why_not_started = _started_by(payload)
    deployer = payload.get("deployer") if isinstance(payload.get("deployer"), str) else None
    if settings.required_approvals:
        if approved is None:
            checks.append(("unknown", f"approvals of {name}: {why_not}"))
        else:
            finished = parse_time(payload.get("finished_at"))
            before = {
                a["username"]
                for a in approved
                if finished is not None
                and (given := parse_time(a.get("created_at"))) is not None
                and given <= finished
            }
            count = len(before)
            if count < settings.required_approvals:
                checks.append(
                    (
                        "fail",
                        f"{name} had {count} approval{'' if count == 1 else 's'} before it was "
                        f"deployed; {settings.required_approvals} required",
                    )
                )
            else:
                checks.append(("pass", f"{name} was approved by {', '.join(sorted(before))}"))
    if approvers:
        if not settings.creator_may_approve:
            if started_by is None:
                checks.append(("unknown", f"{name}: {why_not_started}"))
            elif started_by in approvers:
                checks.append(
                    ("fail", f"{name} was approved by {started_by}, who started the release")
                )
        if not settings.deployer_may_approve:
            if deployer is None:
                checks.append(("unknown", f"GitLab did not record who deployed {name}"))
            elif deployer in approvers:
                checks.append(("fail", f"{name} was deployed by {deployer}, who also approved it"))

    # The change it shipped.
    if settings.require_merge_request_review:
        checks.append(assess.deployed_change(payload, changes, default_branch)[1])

    verdict, reasons = assess.combine(checks)
    return Release(verdict, reasons, started_by, approvers, deployer)
