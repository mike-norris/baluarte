"""The label taxonomy document and drift, computed without a database."""

from datetime import UTC, datetime
from typing import Any

import pytest

from baluarte.labels.drift import (
    Group,
    Inventory,
    OwnerLabels,
    Project,
    compute,
    for_project,
    hierarchies,
)
from baluarte.labels.taxonomy import TaxonomySpec, parse_taxonomy
from baluarte.policy.loader import PolicyInvalid

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

TAXONOMY = """\
apiVersion: baluarte.io/v1alpha1
kind: LabelTaxonomy
metadata:
  name: acme-labels
spec:
  scope:
    groups: ["acme"]
  labels:
    - name: "priority::high"
      color: "#D9534F"
      description: "Must be fixed this sprint"
      aliases: ["P1"]
    - name: bug
      color: "#ff0000"
"""


def spec(source: str = TAXONOMY, **changes: Any) -> TaxonomySpec:
    parsed = parse_taxonomy(source).document.spec
    return parsed.model_copy(update=changes)


ACME = Group(1, 10, None, "acme")
TEAM = Group(2, 11, 10, "acme/team")
OTHER = Group(3, 20, None, "other")
WEB = Project(5, 100, "acme/team/web", 11)
API = Project(6, 101, "acme/api", 10)
INVENTORY = Inventory({g.gitlab_id: g for g in (ACME, TEAM, OTHER)}, [WEB, API])


def label(
    name: str, color: str = "#ff0000", description: str | None = None, id: int = 1
) -> dict[str, Any]:
    return {"id": id, "name": name, "color": color, "description": description}


def observed(*labels: dict[str, Any], status: str = "ok", obs_id: int = 1) -> OwnerLabels:
    return OwnerLabels(
        observation_id=obs_id,
        status=status,
        collected_at=NOW,
        payload_digest="0" * 64 if status == "ok" else None,
        payload={"labels": list(labels)} if status == "ok" else None,
        error=None if status == "ok" else "the token may not read /groups/10/labels (403)",
    )


GOOD_ROOT = observed(
    label("priority::high", "#d9534f", "Must be fixed this sprint", 1), label("bug", id=2)
)


def everyone(**overrides: OwnerLabels) -> dict[tuple[str, int], OwnerLabels]:
    found = {
        ("group", ACME.row_id): GOOD_ROOT,
        ("group", TEAM.row_id): observed(),
        ("project", WEB.row_id): observed(),
        ("project", API.row_id): observed(),
    }
    keys = {
        "acme": ("group", 1),
        "team": ("group", 2),
        "web": ("project", 5),
        "api": ("project", 6),
    }
    for name, value in overrides.items():
        found[keys[name]] = value
    return found


def kinds(items: list[Any]) -> list[tuple[str, str, str | None]]:
    return [(item.owner.path, item.kind, item.label) for item in items]


def test_a_consistent_hierarchy_has_no_drift_and_out_of_scope_groups_are_ignored() -> None:
    assert compute(spec(), INVENTORY, everyone()) == []
    assert [tree.root.path for tree in hierarchies(spec(), INVENTORY)] == ["acme"]


def test_missing_is_reported_once_on_the_root() -> None:
    items = compute(spec(), INVENTORY, everyone(acme=observed(label("bug", id=2))))

    assert kinds(items) == [("acme", "missing", "priority::high")]
    assert items[0].expected == {
        "name": "priority::high",
        "color": "#D9534F",
        "description": "Must be fixed this sprint",
        "placement": {"type": "group", "gitlab_id": 10, "path": "acme"},
    }
    assert items[0].as_dict()["id"] == "group:10:missing:priority::high"


def test_colour_compares_case_insensitively_and_description_exactly() -> None:
    root = observed(label("priority::high", "#000000", "Other", 1), label("bug", "#FF0000", id=2))

    items = compute(spec(), INVENTORY, everyone(acme=root))

    assert kinds(items) == [
        ("acme", "color", "priority::high"),
        ("acme", "description", "priority::high"),
    ]
    assert items[0].label_id == 1


@pytest.mark.parametrize("name", ["P1", "Priority::High", " priority::high ", "p1"])
def test_aliases_and_case_or_spacing_variants_are_misnamed(name: str) -> None:
    root = observed(label(name, "#d9534f", "Must be fixed this sprint", 7), label("bug", id=2))

    items = compute(spec(), INVENTORY, everyone(acme=root))

    assert kinds(items) == [("acme", "misnamed", "priority::high")]
    assert items[0].observed is not None and items[0].observed["name"] == name
    assert items[0].label_id == 7


def test_below_the_root_a_label_is_misplaced_or_a_duplicate() -> None:
    misplaced = compute(
        spec(),
        INVENTORY,
        everyone(acme=observed(label("bug", id=2)), team=observed(label("priority::high", id=9))),
    )
    assert kinds(misplaced) == [
        ("acme", "missing", "priority::high"),
        ("acme/team", "misplaced", "priority::high"),
    ]

    duplicate = compute(spec(), INVENTORY, everyone(web=observed(label("bug", id=3))))
    assert kinds(duplicate) == [("acme/team/web", "duplicate", "bug")]
    assert duplicate[0].reason == "Also defined on acme."


def test_strict_reports_labels_the_taxonomy_does_not_name() -> None:
    labels = everyone(api=observed(label("wontfix", id=4)))

    assert compute(spec(), INVENTORY, labels) == []
    assert kinds(compute(spec(strict=True), INVENTORY, labels)) == [
        ("acme/api", "unknown_label", "wontfix")
    ]


def test_an_unreadable_root_claims_nothing_missing() -> None:
    items = compute(spec(), INVENTORY, everyone(acme=observed(status="forbidden")))

    assert kinds(items) == [("acme", "unknown", None)]
    assert items[0].reason == "the token may not read /groups/10/labels (403)"


def test_an_unread_member_is_unknown_and_the_rest_is_still_judged() -> None:
    labels = everyone(acme=observed(label("bug", id=2)))
    del labels[("project", WEB.row_id)]

    items = compute(spec(), INVENTORY, labels)

    assert kinds(items) == [
        ("acme", "missing", "priority::high"),
        ("acme/team/web", "unknown", None),
    ]


def test_tampered_evidence_is_unknown() -> None:
    root = OwnerLabels(1, "ok", NOW, "0" * 64, {"labels": []}, None, digest_valid=False)

    items = compute(spec(), INVENTORY, everyone(acme=root))

    assert kinds(items) == [("acme", "unknown", None)]


def test_a_project_sees_the_drift_of_its_groups_only() -> None:
    items = compute(
        spec(),
        INVENTORY,
        everyone(acme=observed(label("bug", id=2)), team=observed(label("bug", id=3))),
    )

    assert kinds(for_project(items, INVENTORY, API)) == [("acme", "missing", "priority::high")]
    assert kinds(for_project(items, INVENTORY, WEB)) == [
        ("acme", "missing", "priority::high"),
        ("acme/team", "duplicate", "bug"),
    ]


def test_a_subgroup_scope_makes_the_subgroup_the_root() -> None:
    narrow = spec(TAXONOMY.replace('groups: ["acme"]', 'groups: ["acme/team"]'))

    assert [tree.root.path for tree in hierarchies(narrow, INVENTORY)] == ["acme/team"]


def test_taxonomy_documents_are_validated() -> None:
    with pytest.raises(PolicyInvalid) as error:
        parse_taxonomy(TAXONOMY.replace('"#ff0000"', '"red"').replace('["P1"]', '["bug"]'))
    paths = {e.path for e in error.value.errors}
    assert "spec.labels[1].color" in paths
    with pytest.raises(PolicyInvalid) as repeated:
        parse_taxonomy(TAXONOMY.replace("name: bug", 'name: "Priority::High"'))
    assert "repeats" in repeated.value.errors[0].message
    with pytest.raises(PolicyInvalid):
        parse_taxonomy(TAXONOMY + "    - name: x\n      color: '#000000'\n      extra: 1\n")
