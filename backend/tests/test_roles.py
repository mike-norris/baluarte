import pytest

from baluarte.auth.roles import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    permissions_for,
    roles_from_claim,
)
from baluarte.db.models import ROLES

GROUPS = {
    Role.ADMIN: frozenset({"admins"}),
    Role.ANALYST: frozenset({"analysts", "engineers"}),
    Role.AUDITOR: frozenset(),
}


def test_roles_match_the_database_constraint() -> None:
    assert tuple(Role) == ROLES


def test_every_role_has_permissions() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


@pytest.mark.parametrize(
    ("role", "allowed", "denied"),
    [
        (Role.ADMIN, set(Permission), set()),
        (
            Role.ANALYST,
            {
                Permission.FINDINGS_READ,
                Permission.POLICY_WRITE,
                Permission.EXCEPTION_REQUEST,
                Permission.REMEDIATION_PLAN,
            },
            {
                Permission.SETTINGS_MANAGE,
                Permission.EVIDENCE_EXPORT,
                Permission.AUDIT_LOG_READ,
                Permission.EXCEPTION_APPROVE,
                Permission.REMEDIATION_APPROVE,
            },
        ),
        (
            Role.AUDITOR,
            {Permission.EVIDENCE_EXPORT, Permission.AUDIT_LOG_READ, Permission.EVIDENCE_READ},
            {
                Permission.POLICY_WRITE,
                Permission.SETTINGS_MANAGE,
                Permission.EXCEPTION_REQUEST,
                Permission.EXCEPTION_APPROVE,
                Permission.REMEDIATION_PLAN,
                Permission.REMEDIATION_APPROVE,
            },
        ),
    ],
)
def test_role_matrix(role: Role, allowed: set[Permission], denied: set[Permission]) -> None:
    granted = permissions_for([role])

    assert allowed <= granted
    assert not denied & granted


def test_no_roles_grant_nothing() -> None:
    assert permissions_for([]) == frozenset()


def test_permissions_combine() -> None:
    assert permissions_for([Role.ANALYST, Role.AUDITOR]) == (
        ROLE_PERMISSIONS[Role.ANALYST] | ROLE_PERMISSIONS[Role.AUDITOR]
    )


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        (["engineers", "other"], {Role.ANALYST}),
        (["admins", "analysts"], {Role.ADMIN, Role.ANALYST}),
        ("admins", {Role.ADMIN}),
        (["Admins"], set()),
        ([], set()),
        (None, set()),
        ({"admins": True}, set()),
        ([["admins"], 1], set()),
    ],
)
def test_roles_from_claim(claim: object, expected: set[Role]) -> None:
    assert roles_from_claim(claim, GROUPS) == expected
