"""Prospective DATA equality against real cold P4 reads and SQL consumption.

These tests start after a real P4 publication. They prove the shared preview
and consumer boundary, not the separate pre-effect tool admission contract.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_owed_evidence import read_owed_p4_evidence_on_connection
from elspeth.web.composer.continuation_prepared_p4 import decode_prepared_p4
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.continuation import HistoricalEffectLocator
from elspeth.web.sessions.continuation_p4 import (
    PreparedP4ResultData,
    planned_p4_chat_hash,
    planned_p4_state_hash,
    prepare_p4_receipt_data,
)
from elspeth.web.sessions.continuation_p4_application_store import (
    apply_p4_application_on_connection,
    prepare_p4_application_tail,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table, composition_states_table
from elspeth.web.sessions.state_decoder import state_record_from_row
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_rows
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case
from tests.unit.web.sessions.test_continuation_p4_application_store import continuation_rows
from tests.unit.web.sessions.test_continuation_p4_owner import rows

operation_store = test_composer_operation_authority.operation_store


def retained_rows(engine):
    return stored_rows(engine), rows(engine), continuation_rows(engine)


def prepare_application(store, running, selected, inventory, effect_key, *, prospective=None):
    engine, _repository, _authority, _service, sid = store
    with locked_session_transaction(engine, str(sid)) as conn:
        checked = read_owed_p4_evidence_on_connection(conn, running, selected=selected, inventory=inventory, effect_key=effect_key)
        published_at = database_now(conn)
    receipt = checked.result.receipt
    assert receipt.assistant_id is not None and receipt.authored_state_hash is not None
    result = PreparedP4ResultData(
        receipt,
        checked.result.outcome,
        HistoricalEffectLocator(
            receipt.effect_key,
            receipt.input_sha256,
            receipt.creator_operation_id,
            str(receipt.assistant_id),
            receipt.inserted_state_ids,
            receipt.authored_state_hash,
        ),
    )
    source = selected if prospective is None else prospective
    batch = source.stack.frames[-1]
    return prepare_p4_application_tail(
        running,
        selected=source,
        evidence=checked.snapshot,
        pending_batch=next(chunk for chunk in source.chunks if chunk.reference == batch.accumulator),
        result=result,
        result_chunk=checked.result_chunk,
        published_at=published_at,
        env_ref_names=inventory.names,
    )


def consume_application(store, running, selected, inventory, plan):
    engine, _repository, _authority, _service, sid = store
    compose, batch = selected.stack.frames[0], selected.stack.frames[-1]
    with locked_session_transaction(engine, str(sid)) as conn:
        return apply_p4_application_on_connection(
            conn,
            running,
            selected=selected,
            inventory=inventory,
            expected_evidence_chunk=next(chunk for chunk in selected.chunks if chunk.reference == compose.evidence),
            expected_pending_batch_chunk=next(chunk for chunk in selected.chunks if chunk.reference == batch.accumulator),
            prepared_tail=plan,
        )


@pytest.mark.asyncio
async def test_p4_application_preview_consumes_exact_bytes_after_real_cold_proof(operation_store):
    engine, repository, _authority, _service, _sid = operation_store
    running, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        before = retained_rows(engine)
        plan = prepare_application(operation_store, running, selected, inventory, commit.effect_key)
        assert retained_rows(engine) == before
        applied = consume_application(operation_store, running, selected, inventory, plan)
        assert applied.bundle.entry == plan.bundle.entry
        assert applied.bundle.evidence_chunk == plan.bundle.evidence_chunk
        assert applied.bundle.batch_chunk == plan.bundle.batch_chunk
        assert applied.bundle.audit_chunk == plan.bundle.audit_chunk
        assert applied.bundle.state_chunk == plan.bundle.state_chunk
        assert applied.snapshot.checkpoint_id == plan.append.checkpoint_id
        assert applied.snapshot.payload_json == plan.append.snapshot_json
        assert applied.snapshot.cursor == selected.stack.cursor + 1
        assert rows(engine) == before[1]
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_source", ("checkpoint", "cursor"))
async def test_coherent_altered_preview_source_cannot_replace_actual_sql_authority(operation_store, changed_source):
    engine, repository, _authority, _service, _sid = operation_store
    running, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        before = retained_rows(engine)
        prospective = (
            replace(selected, checkpoint_id=str(uuid4()))
            if changed_source == "checkpoint"
            else replace(selected, stack=selected.stack.model_copy(update={"cursor": selected.stack.cursor + 1}))
        )
        wrong = prepare_application(operation_store, running, selected, inventory, commit.effect_key, prospective=prospective)
        wrong.require_original()
        assert retained_rows(engine) == before
        with pytest.raises(AuditIntegrityError, match="actual P4 application differs"):
            consume_application(operation_store, running, selected, inventory, wrong)
        assert retained_rows(engine) == before
        correct = prepare_application(operation_store, running, selected, inventory, commit.effect_key)
        applied = consume_application(operation_store, running, selected, inventory, correct)
        assert applied.bundle.entry == correct.bundle.entry
        assert rows(engine) == before[1]
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_planned_nominal_row_hashes_reproduce_real_complete_p4_receipt(operation_store):
    engine, repository, _authority, _service, sid = operation_store
    running, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        before = retained_rows(engine)
        with locked_session_transaction(engine, str(sid)) as conn:
            checked = read_owed_p4_evidence_on_connection(
                conn, running, selected=selected, inventory=inventory, effect_key=commit.effect_key
            )
            receipt = checked.result.receipt
            assert receipt.prepared_p4_json is not None
            messages = tuple(
                chat_message_record_from_row(conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == str(mid))).one())
                for mid in receipt.chat_message_ids
            )
            states = tuple(
                state_record_from_row(
                    conn.execute(select(composition_states_table).where(composition_states_table.c.id == str(state_id))).one()
                )
                for state_id in receipt.inserted_state_ids
            )
        rebuilt = prepare_p4_receipt_data(
            effect_key=receipt.effect_key,
            input_sha256=receipt.input_sha256,
            prepared=decode_prepared_p4(receipt.prepared_p4_json, env_ref_names=inventory.names),
            outcome=checked.result.outcome,
            chat_message_ids=tuple(message.id for message in messages),
            chat_message_hashes=tuple(planned_p4_chat_hash(message) for message in messages),
            state_row_hashes=tuple(planned_p4_state_hash(state, creator_operation_id=receipt.creator_operation_id) for state in states),
            env_ref_names=inventory.names,
        )
        assert rebuilt == receipt
        assert retained_rows(engine) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_complete_p4_receipt_reuses_its_initial_prepared_members_and_preserves_public_admission(operation_store, monkeypatch):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer import continuation_p4_outcome as outcomes
    from elspeth.web.composer import continuation_prepared_p4 as prepared_values
    from elspeth.web.sessions.continuation_p4 import decode_p4_receipt

    engine, repository, _authority, _service, sid = operation_store
    running, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        with locked_session_transaction(engine, str(sid)) as conn:
            checked = read_owed_p4_evidence_on_connection(
                conn, running, selected=selected, inventory=inventory, effect_key=commit.effect_key
            )
        receipt = checked.result.receipt
        assert receipt.prepared_p4_json is not None and receipt.audit_outcome_json is not None
        assert receipt.assistant_id is not None and receipt.authored_state_hash is not None
        encoded = canonical_json(receipt.model_dump(mode="json"))
        actual_members = prepared_values._decoded_prepared_p4_members
        actual_projection = outcomes._audit_outcome_from_prepared_members
        attempts, admissions, projections = [], [], []

        def admit(value, *, env_ref_names):
            assert env_ref_names is inventory.names
            attempts.append(value)
            members = actual_members(value, env_ref_names=env_ref_names)
            admissions.append((value, members))
            return members

        def project(value, *, prepared, members, env_ref_names):
            result = actual_projection(value, prepared=prepared, members=members, env_ref_names=env_ref_names)
            projections.append((prepared, members, result))
            return result

        monkeypatch.setattr(prepared_values, "_decoded_prepared_p4_members", admit)
        monkeypatch.setattr(outcomes, "_audit_outcome_from_prepared_members", project)
        before = retained_rows(engine)
        decoded_receipt, decoded_outcome = decode_p4_receipt(encoded, env_ref_names=inventory.names)
        assert decoded_receipt == receipt and decoded_outcome == checked.result.outcome
        assert len(attempts) == len(admissions) == len(projections) == 1
        initial_prepared, initial_members = admissions[0]
        assert attempts[0] is initial_prepared
        assert projections[0][0] is initial_prepared and projections[0][1] is initial_members
        assert projections[0][2] is decoded_outcome

        # Calibrate both sides of the call observer: a pure projection performs
        # no admission; public APIs accepting supplied DATA still admit it.
        semantic = prepared_values._prepared_p4_semantic_input(initial_prepared)
        assert len(attempts) == len(admissions) == 1
        public_members = prepared_values.prepared_p4_members(initial_prepared, env_ref_names=inventory.names)
        assert public_members == initial_members
        assert len(attempts) == len(admissions) == 2
        assert admissions[-1][0] == initial_prepared and admissions[-1][0] is not initial_prepared
        assert admissions[-1][1] is public_members
        assert prepared_values.p4_semantic_input(initial_prepared, env_ref_names=inventory.names) == semantic
        assert len(attempts) == len(admissions) == 3

        malformed = initial_prepared.model_copy(update={"redacted_calls_json": "{}"})
        with pytest.raises(AuditIntegrityError, match="retained P4 calls are not their object array"):
            outcomes.decode_audit_outcome_data(receipt.audit_outcome_json, prepared=malformed, env_ref_names=inventory.names)
        assert len(attempts) == 4 and len(admissions) == 3 and len(projections) == 1

        # Public outcome parsing must still precede supplied-prepared admission.
        # Compare against the real malformed-outcome/valid-prepared control.
        with pytest.raises(AuditIntegrityError) as invalid_outcome:
            outcomes.decode_audit_outcome_data("{}", prepared=initial_prepared, env_ref_names=inventory.names)
        with pytest.raises(AuditIntegrityError) as both_invalid:
            outcomes.decode_audit_outcome_data("{}", prepared=malformed, env_ref_names=inventory.names)
        assert str(both_invalid.value) == str(invalid_outcome.value)
        assert len(attempts) == 4 and len(admissions) == 3 and len(projections) == 1
        assert retained_rows(engine) == before
    finally:
        repository.release(running.session_operation_context)
