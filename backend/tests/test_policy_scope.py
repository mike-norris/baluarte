import pytest

from baluarte.policy.assignments import in_scope


@pytest.mark.parametrize(
    ("groups", "namespace", "expected"),
    [
        (["*"], "platform/backend", True),
        (["*"], "alice", True),
        (["platform"], "platform", True),
        (["platform"], "platform/backend/api", True),
        (["platform/backend"], "platform", False),
        (["platform/back"], "platform/backend", False),
        (["Platform/Backend"], "platform/backend/api", True),
        (["other", "platform/backend"], "platform/backend", True),
        (["other"], "platform", False),
    ],
)
def test_scope_covers_a_group_and_its_subgroups(
    groups: list[str], namespace: str, expected: bool
) -> None:
    assert in_scope(groups, namespace) is expected
