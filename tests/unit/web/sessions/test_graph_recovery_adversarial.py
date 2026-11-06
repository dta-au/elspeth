"""Adversarial controls for recovery admission before graph hydration."""

import json
from datetime import UTC, datetime
from json import JSONDecodeError
from sys import getrecursionlimit
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.state import CompositionState
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.graph_recovery import empty_recovery_state
from elspeth.web.sessions.models import chat_messages_table, composition_graph_parks_table, composition_states_table
from elspeth.web.sessions.schemas import SendMessageRequest
from elspeth.web.sessions.state_envelope import envelope_state_column
from tests.helpers.composition_state import stamped_state_values, stored_composer_meta
from tests.unit.web.coordination.test_composer_operation_authority import admit
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store_fixture
from tests.unit.web.coordination.test_graph_recovery_persistence import begin, finish


@pytest.fixture
def operation_store(tmp_path):
    yield from _operation_store_fixture.__wrapped__(tmp_path)


def _insert_unreadable_head(engine, session_id, *, corruption: str, before_corruption=None):
    """Corrupt only graph bytes; session and operation authority stay intact.

    The head is first written readable, with the content publication a real
    writer stamps. ``before_corruption(state_id)`` runs against that readable
    head (Continuation admission hydrates its bound base, so a request is
    admitted before the damage); only then are the graph bytes corrupted,
    bypassing the JSON encoder so corruption can fail in the result processor
    before _row_to_state_record ever runs.
    """
    state_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                **stamped_state_values(
                    id=str(state_id),
                    session_id=str(session_id),
                    version=1,
                    sources=envelope_state_column({}),
                    nodes=envelope_state_column([]),
                    edges=envelope_state_column([]),
                    outputs=envelope_state_column([]),
                    metadata_=envelope_state_column({"name": "Draft", "description": ""}),
                    is_valid=False,
                    created_at=datetime.now(UTC),
                    provenance="session_seed",
                )
            )
        )
    if before_corruption is not None:
        before_corruption(state_id)
    depth = getrecursionlimit() + 100
    column, raw_json = {
        "invalid-envelope": ("nodes", '{"missing_version_envelope": true}'),
        "invalid-json": ("nodes", "{invalid-json"),
        "excessive-json-depth": ("nodes", '{"_version":1,"data":' + "[" * depth + "0" + "]" * depth + "}"),
        "wrong-sources-shape": ("sources", '{"_version": 1, "data": ["not-a-source-map"]}'),
    }[corruption]
    with engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE composition_states SET {column} = ? WHERE id = ?", (raw_json, str(state_id)))
        if corruption == "wrong-sources-shape":
            # Re-bind the publication to the damaged bytes, so the shape check
            # (not the content-publication check) is what refuses the row.
            meta = stored_composer_meta(
                session_id=session_id,
                state_id=state_id,
                version=1,
                sources=["not-a-source-map"],
                nodes=[],
                edges=[],
                outputs=[],
                metadata={"name": "Draft", "description": ""},
            )
            conn.exec_driver_sql("UPDATE composition_states SET composer_meta = ? WHERE id = ?", (json.dumps(meta), str(state_id)))
    return state_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corruption", "decode_failure"),
    [
        ("invalid-envelope", AuditIntegrityError),
        ("invalid-json", JSONDecodeError),
        ("excessive-json-depth", RecursionError),
        ("wrong-sources-shape", ValueError),
    ],
)
async def test_malformed_head_recovery_keeps_exact_fence_and_ignores_unsaveable_candidate(
    operation_store, corruption, decode_failure, monkeypatch
):
    engine, repository, authority, service, session_id = operation_store
    admitted = []

    def admit_on_readable_head(state_id):
        # Continuation admission hydrates the bound base, so the request is
        # admitted while the head is readable and the damage lands afterwards.
        request = SendMessageRequest(operation_id=str(uuid4()), content="Stop and recover the draft", state_id=state_id)
        admitted.append(admit(authority, session_id, request))

    state_id = _insert_unreadable_head(engine, session_id, corruption=corruption, before_corruption=admit_on_readable_head)
    ((record, fresh),) = admitted
    assert fresh and record.base_state_id == state_id
    # Positive control: this fixture actually defeats ordinary head hydration.
    with pytest.raises(decode_failure):
        broken_record = await service.get_current_state(session_id)
        assert broken_record is not None
        state_from_record(broken_record)
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    try:
        assert context.operation_kind is SessionOperationKind.COMPOSE
        assert context.fence.session_id == str(session_id)
        running = authority.get(session_id=session_id, operation_id=record.operation_id)
        assert running is not None and running.status == "running"
        assert running.session_operation_id == context.fence.operation_id
        assert running.session_operation_epoch == context.fence.operation_epoch
        admission = await service.load_graph_recovery_context(session_id, turn_start_state_id=state_id, session_operation_context=context)
        assert admission.record is None
        assert admission.context.malformed_head
        assert admission.context.current_state_id == str(state_id)
        assert admission.context.current_version == 1
        assert admission.context.turn_start_state_id == str(state_id)
        assert admission.context.turn_start_version == 1
        abandoned_candidate = empty_recovery_state()
        original_to_dict = CompositionState.to_dict

        def reject_abandoned_serialization(self):
            if self is abandoned_candidate:
                raise AssertionError("The abandoned candidate must not be serialized")
            return original_to_dict(self)

        monkeypatch.setattr(CompositionState, "to_dict", reject_abandoned_serialization)
        # Positive control proves the abandoned draft really cannot be saved.
        with pytest.raises(AssertionError, match="abandoned candidate"):
            abandoned_candidate.to_dict()
        recovery = await service.prepare_graph_recovery(
            session_id,
            context=admission.context,
            intent=GraphRecoveryIntent(
                action="reset_to_safe_checkpoint", message="Let's start over.", park_id=None, tool_call_id="recover-test"
            ),
            session_operation_context=context,
            candidate_state=abandoned_candidate,
        )
        assert recovery.empty_fallback
        assert recovery.expected_state_id == state_id
        assert recovery.target_state_id is None
        assert recovery.data is not None and not recovery.data.is_valid
        with pytest.raises(AuditIntegrityError, match="bound base"):
            await service.load_graph_recovery_context(session_id, turn_start_state_id=uuid4(), session_operation_context=context)
        with pytest.raises(SessionOperationFenceLost):
            await service.load_graph_recovery_context(uuid4(), turn_start_state_id=None, session_operation_context=context)
        with engine.connect() as conn:
            assert conn.execute(select(composition_states_table.c.id)).scalars().all() == [str(state_id)]
    finally:
        repository.release(context)
    with pytest.raises(SessionOperationFenceLost):
        await service.load_graph_recovery_context(session_id, turn_start_state_id=state_id, session_operation_context=context)


@pytest.mark.asyncio
async def test_completed_recovery_replays_after_fence_release_without_new_writes(operation_store):
    engine, repository, _authority, service, session_id = operation_store
    running, admission = await begin(operation_store)
    settlement = await service.prepare_graph_recovery(
        session_id,
        context=admission.context,
        intent=GraphRecoveryIntent("park", "Let's discuss the goal.", None, "replay-park"),
        session_operation_context=running.session_operation_context,
    )
    terminal = await finish(operation_store, running, settlement)
    assert terminal.status == "completed"
    tables = (composition_states_table, composition_graph_parks_table, chat_messages_table)
    with engine.connect() as conn:
        before = tuple(tuple(conn.execute(select(table)).all()) for table in tables)
    repository.release(running.session_operation_context)

    def refuse_rebuilding_response(message, proposals, state):
        raise AssertionError("A completed recovery must replay its durable response")

    replay = await finish(operation_store, running, settlement, build_response=refuse_rebuilding_response)
    assert replay == terminal
    with engine.connect() as conn:
        after = tuple(tuple(conn.execute(select(table)).all()) for table in tables)
    assert after == before


@pytest.mark.asyncio
async def test_graph_read_never_attributes_old_damage_to_newer_healthy_head(operation_store, monkeypatch):
    engine, _repository, _authority, service, session_id = operation_store
    damaged_id = _insert_unreadable_head(engine, session_id, corruption="invalid-envelope")
    healthy_id = uuid4()
    original_convert = service._row_to_state_record
    conversion_ids = []
    read_connections = []

    def observe_read_connection(conn, cursor, statement, parameters, context, executemany):
        read_connections.append(conn)

    def publish_new_head_before_old_conversion(row):
        conversion_ids.append(row.id)
        if row.id == str(damaged_id):
            # SQLite's BEGIN IMMEDIATE serializes external writers. Inject
            # the interleaving through this connection to test identity
            # binding independently of that SQLite-only exclusion.
            read_connections[-1].execute(
                insert(composition_states_table).values(
                    **stamped_state_values(
                        id=str(healthy_id),
                        session_id=str(session_id),
                        version=2,
                        sources=envelope_state_column({}),
                        nodes=envelope_state_column([]),
                        edges=envelope_state_column([]),
                        outputs=envelope_state_column([]),
                        metadata_=envelope_state_column({"name": "Recovered", "description": ""}),
                        is_valid=False,
                        created_at=datetime.now(UTC),
                        provenance="session_seed",
                    )
                )
            )
        return original_convert(row)

    monkeypatch.setattr(service, "_row_to_state_record", publish_new_head_before_old_conversion)
    event.listen(engine, "before_cursor_execute", observe_read_connection)
    try:
        damaged_read = await service.read_current_composition_graph(session_id)
    finally:
        event.remove(engine, "before_cursor_execute", observe_read_connection)
    assert conversion_ids == [str(damaged_id)]
    assert damaged_read.hydration_error == "AuditIntegrityError"
    assert damaged_read.record is None
    assert damaged_read.head.state_id == damaged_id and damaged_read.head.version == 1
    healthy_read = await service.read_current_composition_graph(session_id)
    assert healthy_read.hydration_error is None
    assert healthy_read.record is not None and healthy_read.record.id == healthy_id
    assert healthy_read.head.state_id == healthy_id and healthy_read.head.version == 2
