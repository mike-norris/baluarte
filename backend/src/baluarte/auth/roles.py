"""Roles, permissions and the mapping from identity provider groups to roles.

Endpoints check permissions, never roles, so the role matrix lives in one place.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    AUDITOR = "auditor"
    # Follows posture, consistency and effort across the groups they own; changes no baseline
    # or setting.
    MANAGER = "manager"


class Permission(StrEnum):
    INVENTORY_READ = "inventory:read"
    FINDINGS_READ = "findings:read"
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_EXPORT = "evidence:export"
    # Build an audit-period evidence package with a reproducible sample of changes.
    EVIDENCE_PACKAGE = "evidence:package"
    # Add evidence items (uploads and references), edit and submit one's own drafts, and check
    # references (roadmap M15.2).
    EVIDENCE_MANAGE = "evidence:manage"
    # Accept or reject submitted evidence items (never one's own) and read restricted ones.
    EVIDENCE_REVIEW = "evidence:review"
    POLICY_WRITE = "policy:write"
    # Ask for a time-limited waiver of a failing rule.
    EXCEPTION_REQUEST = "exception:request"
    # Approve, reject or revoke one; never one's own request (enforced separately).
    EXCEPTION_APPROVE = "exception:approve"
    # Draft and submit remediation plans, and apply approved ones while remediation writes are
    # switched on (ADR-0012).
    REMEDIATION_PLAN = "remediation:plan"
    # Approve or reject a submitted plan; never one's own (enforced separately).
    REMEDIATION_APPROVE = "remediation:approve"
    # Build template change sets, save selections, and withdraw one's own proposed set.
    CHANGES_PLAN = "changes:plan"
    # Approve or reject change sets (never one's own), and apply, continue, cancel or roll back
    # approved ones while template writes are switched on.
    CHANGES_APPROVE = "changes:approve"
    SETTINGS_MANAGE = "settings:manage"
    AUDIT_LOG_READ = "audit_log:read"
    JOBS_READ = "jobs:read"
    # The operating profile, Baluarte's posture against it, its exceptions and attestations.
    COMPLIANCE_READ = "compliance:read"
    # The manager space: overview and trends across groups and tags.
    MANAGER_READ = "manager:read"
    # Time spent and estimated, as totals.
    TIME_READ = "time:read"
    # Time per person, only for projects in the reader's manager scope (admins: all).
    TIME_READ_PEOPLE = "time:read_people"
    # Create Baluarte tags and assign them to groups and projects; never changes GitLab.
    TAGS_WRITE = "tags:write"
    # Change the auditable issue register: selection rules, and including or excluding an issue
    # with a reason. Reading it needs only evidence:read.
    REGISTER_WRITE = "register:write"


_READ = frozenset({Permission.INVENTORY_READ, Permission.FINDINGS_READ, Permission.EVIDENCE_READ})

ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.ANALYST: _READ
    | {
        Permission.POLICY_WRITE,
        Permission.EXCEPTION_REQUEST,
        Permission.REMEDIATION_PLAN,
        Permission.CHANGES_PLAN,
        Permission.EVIDENCE_PACKAGE,
        Permission.MANAGER_READ,
        Permission.TIME_READ,
        Permission.TAGS_WRITE,
        Permission.REGISTER_WRITE,
        Permission.EVIDENCE_MANAGE,
    },
    Role.AUDITOR: _READ
    | {
        Permission.EVIDENCE_MANAGE,
        Permission.EVIDENCE_REVIEW,
        Permission.EVIDENCE_EXPORT,
        Permission.EVIDENCE_PACKAGE,
        Permission.AUDIT_LOG_READ,
        Permission.COMPLIANCE_READ,
        Permission.MANAGER_READ,
        Permission.TIME_READ,
        Permission.REGISTER_WRITE,
    },
    Role.MANAGER: _READ
    | {
        Permission.MANAGER_READ,
        Permission.TIME_READ,
        Permission.TIME_READ_PEOPLE,
        Permission.TAGS_WRITE,
    },
}


def permissions_for(roles: Iterable[Role]) -> frozenset[Permission]:
    granted: frozenset[Permission] = frozenset()
    for role in roles:
        granted |= ROLE_PERMISSIONS[role]
    return granted


def roles_from_claim(claim: object, group_roles: Mapping[Role, frozenset[str]]) -> frozenset[Role]:
    """Map a groups claim to roles. Anything other than a string or list of strings grants none."""
    if isinstance(claim, str):
        groups = {claim}
    elif isinstance(claim, list):
        groups = {item for item in claim if isinstance(item, str)}
    else:
        return frozenset()
    return frozenset(role for role, mapped in group_roles.items() if groups & mapped)
