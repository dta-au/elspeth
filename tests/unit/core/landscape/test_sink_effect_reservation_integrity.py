"""Reservation admission binds token identity, current state and target stream."""

from dataclasses import replace

import pytest
from sqlalchemy import delete, select, update

from elspeth.contracts.hashing import stable_hash
from elspeth.core.landscape.schema import (
    node_states_table,
    operations_table,
    sink_effect_members_table,
    sink_effect_streams_table,
    sink_effects_table,
)
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members, _pipeline_request


@pytest.fixture
def db_factory():
    db = make_landscape_db()
    try:
        yield db, make_factory(db)
    finally:
        db.close()


def _durable_state(db):
    with db.read_only_connection() as conn:
        return tuple(
            tuple(sorted(tuple(row) for row in conn.execute(select(table)).fetchall()))
            for table in (sink_effects_table, sink_effect_members_table, sink_effect_streams_table, operations_table)
        )


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("token_id", "missing-token", "tokens are missing"),
        ("row_id", "missing-row", "row/run identity"),
        ("ingest_sequence", 17, "row/run identity"),
        ("payload_hash", "e" * 64, "payload is divergent"),
    ],
)
def test_reservation_refuses_member_facts_that_do_not_match_durable_identity(db_factory, field, value, message):
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 1)
    altered = (
        replace(members[0], row={"ordinal": 99}, payload_hash=stable_hash({"ordinal": 99}))
        if field == "payload_hash"
        else replace(members[0], **{field: value})
    )
    request = _pipeline_request(run_id, sink_id, (altered,), replacing_target=True)
    token = leader_coordination_token(factory, run_id)
    before = _durable_state(db)
    with pytest.raises(ValueError, match=message):
        factory.execution.sink_effects.reserve(request, coordination_token=token)
    assert _durable_state(db) == before


def test_reservation_requires_current_sink_state(db_factory):
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 1)
    with db.engine.begin() as conn:
        conn.execute(delete(node_states_table).where(node_states_table.c.token_id == members[0].token_id))
    token = leader_coordination_token(factory, run_id)
    before = _durable_state(db)
    with pytest.raises(ValueError, match="no current sink-node state"):
        factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members, replacing_target=True), coordination_token=token)
    assert _durable_state(db) == before


def test_reservation_refuses_cross_run_leader_token_before_mutation(db_factory):
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 1)
    other_run_id, _other_sink_id, _other_members = _pipeline_members(factory, 1)
    token = leader_coordination_token(factory, other_run_id)
    before = _durable_state(db)
    with pytest.raises(ValueError, match="different run"):
        factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members), coordination_token=token)
    assert _durable_state(db) == before


@pytest.mark.parametrize("original_replacing", [False, True])
def test_rebatch_cannot_change_existing_replacing_target_membership(db_factory, original_replacing):
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 2)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, run_id)
    original = repo.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=original_replacing), coordination_token=token)
    assert original.new_effect is not None
    before = _durable_state(db)
    with pytest.raises(ValueError, match=r"stream membership|stream-bound"):
        repo.reserve(_pipeline_request(run_id, sink_id, members, replacing_target=not original_replacing), coordination_token=token)
    assert _durable_state(db) == before
    assert repo.get_members(original.new_effect.effect_id)[0].token_id == members[0].token_id


@pytest.mark.parametrize("field, value", [("payload_hash", "e" * 64), ("ingest_sequence", 55)])
def test_bound_member_corruption_refuses_new_unbound_members_atomically(db_factory, field, value):
    db, factory = db_factory
    run_id, sink_id, members = _pipeline_members(factory, 2)
    repo = factory.execution.sink_effects
    token = leader_coordination_token(factory, run_id)
    effect = repo.reserve(_pipeline_request(run_id, sink_id, members[:1], replacing_target=True), coordination_token=token).new_effect
    assert effect is not None
    # Simulate corrupted audit metadata while keeping the actual token and
    # current-state witnesses intact. Admission must not repair the old row.
    with db.engine.begin() as conn:
        conn.execute(
            update(sink_effect_members_table).where(sink_effect_members_table.c.effect_id == effect.effect_id).values(**{field: value})
        )
    before = _durable_state(db)
    with pytest.raises(ValueError, match="divergent immutable membership"):
        repo.reserve(_pipeline_request(run_id, sink_id, members, replacing_target=True), coordination_token=token)
    assert _durable_state(db) == before
