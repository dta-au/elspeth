"""Hand-written Continuation closed-value CHECKs stay equal to their Literal owners.

``models.py`` writes journal ``tag``, segment ``rollover_reason`` and effect ``owner``
CHECKs as literal SQL so a contract edit cannot change what the database
admits without an epoch bump. These tests derive the expected value set from
``get_args`` and compare it with the values the live CHECK names, and they
prove each CHECK admits every value and refuses a value outside the set.
Control: add a value to a Literal, or drop one from its CHECK -> red.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import CheckConstraint, Column, MetaData, String, Table, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from elspeth.web.composer.continuation_handoff import ContinuationYieldReason
from elspeth.web.sessions import models
from elspeth.web.sessions.continuation_events import ContinuationEffectOwner, ContinuationEventTag
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    SESSION_SCHEMA_EPOCH,
    composer_continuation_effects_table,
    composer_continuation_events_table,
    composer_operation_segments_table,
    schema_identity_table,
)
from elspeth.web.sessions.schema import SessionSchemaError
from tests.unit.web.coordination import test_composer_operation_authority as operation_fixtures

operation_store = operation_fixtures.operation_store


def _quoted_values(sql: str) -> list[str]:
    return re.findall(r"'([^']*)'", sql)


def _live_check(table: Table, name: str) -> str:
    (constraint,) = [item for item in table.constraints if isinstance(item, CheckConstraint) and item.name == name]
    return str(constraint.sqltext)


@pytest.mark.parametrize(
    ("sql", "table", "name", "owner"),
    [
        (models._CONTINUATION_EVENT_TAG_CHECK, composer_continuation_events_table, "ck_composer_events_tag", ContinuationEventTag),
        (
            models._CONTINUATION_EFFECT_OWNER_CHECK,
            composer_continuation_effects_table,
            "ck_composer_effects_owner",
            ContinuationEffectOwner,
        ),
        (
            models._CONTINUATION_ROLLOVER_REASON_CHECK,
            composer_operation_segments_table,
            "ck_composer_segments_rollover_reason",
            ContinuationYieldReason,
        ),
    ],
    ids=["event-tag", "effect-owner", "rollover-reason"],
)
def test_check_names_exactly_the_literal_values(sql, table, name, owner) -> None:
    values = _quoted_values(sql)
    assert len(values) == len(set(values))
    assert set(values) == set(get_args(owner))
    assert _live_check(table, name) == sql


@pytest.mark.parametrize(
    ("sql", "column", "owner", "nullable"),
    [
        (models._CONTINUATION_EVENT_TAG_CHECK, "tag", ContinuationEventTag, False),
        (models._CONTINUATION_EFFECT_OWNER_CHECK, "owner", ContinuationEffectOwner, False),
        (models._CONTINUATION_ROLLOVER_REASON_CHECK, "rollover_reason", ContinuationYieldReason, True),
    ],
    ids=["event-tag", "effect-owner", "rollover-reason"],
)
def test_check_admits_each_value_and_refuses_an_outside_value(tmp_path: Path, sql, column, owner, nullable) -> None:
    metadata = MetaData()
    probe = Table("probe", metadata, Column(column, String, nullable=True), CheckConstraint(sql, name="ck_probe"))
    engine = create_session_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            for value in get_args(owner):
                conn.execute(insert(probe).values({column: value}))
            if nullable:
                conn.execute(insert(probe).values({column: None}))
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(insert(probe).values({column: "not_a_member"}))
    finally:
        engine.dispose()


def _effect_values(record, running, *, key: str, owner: str) -> dict[str, object]:
    return {
        "session_id": str(running.claim.session_id),
        "operation_id": record.operation_id,
        "effect_key": key,
        "input_sha256": "0" * 64,
        "owner": owner,
        "disposition": "prepared",
        "creator_sequence": running.claim.segment_sequence,
        "creator_generation": running.claim.ownership_generation,
    }


def test_actual_effects_table_admits_all_closed_owners_on_insert_and_update(operation_store) -> None:
    engine, _repository, _authority, _service, _sid = operation_store
    record, running = operation_fixtures.start(operation_store)
    owners = get_args(ContinuationEffectOwner)
    effects = composer_continuation_effects_table
    with engine.begin() as conn:
        for owner in owners:
            conn.execute(insert(effects).values(**_effect_values(record, running, key=f"owner:{owner}", owner=owner)))
        assert Counter(conn.execute(select(effects.c.owner)).scalars()) == Counter(owners)
        target = effects.c.effect_key == f"owner:{owners[0]}"
        original = conn.execute(select(effects).where(target)).one()._asdict()
        for owner in owners:
            assert conn.execute(update(effects).where(target).values(owner=owner)).rowcount == 1
            assert conn.execute(select(effects).where(target)).one()._asdict() == {**original, "owner": owner}


@pytest.mark.parametrize("mutation", ["insert", "update"])
def test_actual_effects_table_refuses_outside_owner_values_without_changing_rows(operation_store, mutation: str) -> None:
    engine, _repository, _authority, _service, _sid = operation_store
    record, running = operation_fixtures.start(operation_store)
    effects = composer_continuation_effects_table
    with engine.begin() as conn:
        conn.execute(insert(effects).values(**_effect_values(record, running, key="owner:retained", owner="provider")))
        before = conn.execute(select(effects).order_by(effects.c.effect_key)).all()
        for owner in ("", "primary_turn", "review", "provider:3", "not_a_member"):
            with pytest.raises(IntegrityError, match="ck_composer_effects_owner"), conn.begin_nested():
                if mutation == "insert":
                    conn.execute(insert(effects).values(**_effect_values(record, running, key="owner:invalid", owner=owner)))
                else:
                    conn.execute(update(effects).where(effects.c.effect_key == "owner:retained").values(owner=owner))
            assert conn.execute(select(effects).order_by(effects.c.effect_key)).all() == before


@pytest.mark.parametrize("change", ["missing", "weakened"])
def test_effect_owner_check_drift_refuses_epoch_80_and_preserves_private_store(operation_store, change: str) -> None:
    engine, _repository, _authority, _service, _sid = operation_store
    record, running = operation_fixtures.start(operation_store)
    effects = composer_continuation_effects_table
    constraint = "ck_composer_effects_owner"
    url = engine.url.render_as_string()
    with engine.begin() as conn:
        conn.execute(insert(effects).values(**_effect_values(record, running, key="owner:preserved", owner="provider")))
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'composer_continuation_effects'")
        ).scalar_one()
        clause = f"CONSTRAINT {constraint} CHECK ({models._CONTINUATION_EFFECT_OWNER_CHECK})"
        assert ddl.count(clause) == 1
        if change == "missing":
            assert ddl.count(clause + ",") == 1
            changed = ddl.replace(clause + ",", "", 1)
        else:
            assert change == "weakened"
            changed = ddl.replace(clause, f"CONSTRAINT {constraint} CHECK (1 = 1)", 1)
        assert changed != ddl
        conn.exec_driver_sql("PRAGMA writable_schema = ON")
        try:
            conn.execute(
                text("UPDATE sqlite_master SET sql = :sql WHERE type = 'table' AND name = 'composer_continuation_effects'"),
                {"sql": changed},
            )
        finally:
            conn.exec_driver_sql("PRAGMA writable_schema = OFF")
    engine.dispose()
    stale = create_session_engine(url, read_only=True)
    try:
        with stale.connect() as conn:
            schema_before = conn.execute(text("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")).all()
            identities_before = conn.execute(select(schema_identity_table)).all()
            effects_before = conn.execute(select(effects).order_by(effects.c.effect_key)).all()
            assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == SESSION_SCHEMA_EPOCH == 83
            assert identities_before[0].schema_epoch == 83
        with pytest.raises(SessionSchemaError) as raised:
            unexpectedly_admitted = create_session_engine(url)
            unexpectedly_admitted.dispose()
        assert constraint in str(raised.value)
        expected = "CHECK constraint SQL mismatch" if change == "weakened" else "CHECK constraint mismatch"
        assert expected in str(raised.value)
        with stale.connect() as conn:
            assert conn.execute(text("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")).all() == schema_before
            assert conn.execute(select(schema_identity_table)).all() == identities_before
            assert conn.execute(select(effects).order_by(effects.c.effect_key)).all() == effects_before
            assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 83
    finally:
        stale.dispose()
