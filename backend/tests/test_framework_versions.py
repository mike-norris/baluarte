# ruff: noqa: F811 (pytest fixtures imported from other test modules)
"""Framework versions as data (roadmap M15.1): the catalogue in the database matches the code,
catalogue rows never change, records pin the version they were made against, a new version is
added beside the old, and an organization may describe a control in its own words."""

from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_control_mapping import (  # noqa: F401
    CHANGES,
    World,
    analyst,
    auditor,
    gitlab_env,
    in_days,
    propose,
    world,
)

from baluarte.compliance.catalogue import FRAMEWORK_IDS
from baluarte.controls import versions
from baluarte.controls.catalogue import CONTROLS, EDITIONS
from baluarte.db.models import AuditEvent, ControlMappingChange, FrameworkVersion

pytestmark = pytest.mark.db

DESCRIBE = "/api/v1/frameworks/soc2/2017-r2022/controls/CC8.1/description"


def test_the_seeded_catalogue_matches_the_code(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as db:
        for framework in FRAMEWORK_IDS:
            current = versions.current(db, framework)
            assert current.edition == EDITIONS[framework]
            stored = {
                (c.control_id, c.title, c.area, c.text_source)
                for c in versions.controls(db, current.id)
            }
            expected = {
                (c.id, c.title, c.area, "baluarte") for c in CONTROLS if c.framework == framework
            }
            assert stored == expected, framework
        assert {v.framework: v.text_redistributable for v in versions.versions(db)} == {
            "soc2": False,
            "iso27001": False,
            "nist_800_53_moderate": True,
        }


def test_catalogue_rows_never_change_except_which_version_is_current(
    migrated_engine: Engine,
) -> None:
    for statement in (
        "UPDATE framework_control SET title = 'Anything' WHERE control_id = 'CC8.1'",
        "DELETE FROM framework_control WHERE control_id = 'CC8.1'",
        "UPDATE framework_version SET edition = 'Anything' WHERE framework = 'soc2'",
        "DELETE FROM framework_version WHERE framework = 'soc2'",
    ):
        with pytest.raises(DBAPIError, match="cannot"):
            with migrated_engine.begin() as connection:
                connection.execute(text(statement))
    with migrated_engine.begin() as connection:
        connection.execute(text("UPDATE framework_version SET current = false"))
        connection.execute(text("UPDATE framework_version SET current = true"))


def test_frameworks_are_listed_with_their_controls(migrated_engine: Engine) -> None:
    listed = auditor(migrated_engine).get("/api/v1/frameworks")
    assert listed.status_code == 200, listed.text
    counts = {(v["framework"], v["version"]): v["controls"] for v in listed.json()["versions"]}
    assert counts == {
        ("soc2", "2017-r2022"): sum(1 for c in CONTROLS if c.framework == "soc2"),
        ("iso27001", "2022"): sum(1 for c in CONTROLS if c.framework == "iso27001"),
        ("nist_800_53_moderate", "rev5-moderate"): sum(
            1 for c in CONTROLS if c.framework == "nist_800_53_moderate"
        ),
    }
    one = auditor(migrated_engine).get("/api/v1/frameworks/iso27001/2022")
    assert one.status_code == 200, one.text
    assert one.json()["current"] is True
    assert {c["control_id"] for c in one.json()["items"]} >= {"A.8.32", "A.5.15"}
    assert auditor(migrated_engine).get("/api/v1/frameworks/iso27001/2013").status_code == 404


def test_an_organization_describes_a_control_in_its_own_words(migrated_engine: Engine) -> None:
    words = "Our change advisory board approves every production change; see the CAB charter."
    assert auditor(migrated_engine).put(DESCRIBE, json={"description": words}).status_code == 403
    described = analyst(migrated_engine).put(DESCRIBE, json={"description": f"  {words} "})
    assert described.status_code == 200, described.text
    assert described.json()["description"]["text"] == words
    assert described.json()["title"] == "Changes are authorized, tested and approved before release"

    def in_force() -> Any:
        body = auditor(migrated_engine).get("/api/v1/frameworks/soc2/2017-r2022").json()
        (found,) = [c for c in body["items"] if c["control_id"] == "CC8.1"]
        return found["description"]

    assert in_force()["text"] == words
    detail = auditor(migrated_engine).get("/api/v1/controls/soc2/CC8.1").json()
    assert detail["description"] == words
    assert detail["framework_version"] == "2017-r2022"

    assert analyst(migrated_engine).put(DESCRIBE, json={"description": ""}).status_code == 200
    assert in_force() is None
    assert (
        analyst(migrated_engine)
        .put(
            "/api/v1/frameworks/soc2/2017-r2022/controls/CC9.9/description",
            json={"description": "x"},
        )
        .status_code
        == 404
    )
    with Session(migrated_engine) as db:
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_type == "framework_control")
        ).all()
    assert actions == ["framework_control.described", "framework_control.description_cleared"]
    for statement in (
        "UPDATE framework_control_description SET description = 'x'",
        "DELETE FROM framework_control_description",
    ):
        with pytest.raises(DBAPIError, match="cannot be changed or deleted"):
            with migrated_engine.begin() as connection:
                connection.execute(text(statement))


def test_records_keep_their_version_when_a_new_one_is_added(world: World) -> None:
    engine = world.engine
    first = propose(
        engine, framework="soc2", control_id="CC6.8", kind="not_applicable", review_by=in_days(30)
    )
    assert first.status_code == 201, first.text
    assert first.json()["framework_version"] == "2017-r2022"

    # A later edition is added beside the current one and becomes current.
    with Session(engine) as db:
        old = versions.current(db, "soc2")
        old.current = False
        db.flush()
        db.add(
            FrameworkVersion(
                framework="soc2",
                version="2027",
                edition="A later edition",
                publisher="AICPA",
                licence_note="Paraphrased.",
                text_redistributable=False,
                current=True,
            )
        )
        db.commit()
    second = propose(
        engine, framework="soc2", control_id="CC7.1", kind="not_applicable", review_by=in_days(30)
    )
    assert second.status_code == 201, second.text
    assert second.json()["framework_version"] == "2027"
    history = analyst(engine).get(CHANGES).json()["items"]
    assert {c["id"]: c["framework_version"] for c in history} == {
        first.json()["id"]: "2017-r2022",
        second.json()["id"]: "2027",
    }

    with pytest.raises(DBAPIError, match="cannot change"):
        with Session(engine) as db:
            row = db.get_one(ControlMappingChange, first.json()["id"])
            row.framework_version_id = versions.current_version_id(db, "soc2")
            db.commit()
