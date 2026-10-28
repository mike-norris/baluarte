"""Roles, permissions and the mapping from identity provider groups to roles.

Endpoints check permissions, never roles, so the role matrix lives in one place.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    AUDITOR = "auditor"


class Permission(StrEnum):
    INVENTORY_READ = "inventory:read"
    FINDINGS_READ = "findings:read"
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_EXPORT = "evidence:export"
    POLICY_WRITE = "policy:write"
    SETTINGS_MANAGE = "settings:manage"
    AUDIT_LOG_READ = "audit_log:read"


_READ = frozenset({Permission.INVENTORY_READ, Permission.FINDINGS_READ, Permission.EVIDENCE_READ})

ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.ANALYST: _READ | {Permission.POLICY_WRITE},
    Role.AUDITOR: _READ | {Permission.EVIDENCE_EXPORT, Permission.AUDIT_LOG_READ},
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
