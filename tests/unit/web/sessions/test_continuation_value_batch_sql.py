"""Actual V4 paid-source ordered batch SQL, cold and rollback opponents."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_value_batch_computation as computation
from elspeth.web.composer import continuation_value_batch_preparation as preparation
from elspeth.web.composer import tool_batch
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.sessions import continuation_value_batch_tail as tail
from elspeth.web.sessions.continuation import continuation_unpaid_terminal_reserve_on_connection
from elspeth.web.sessions.continuation_prepared_tail import prepared_append_chunks
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_value_batch_read import read_value_batch_application_on_connection
from elspeth.web.sessions.continuation_value_batch_refusal import read_value_batch_preparation_refusal_on_connection
from elspeth.web.sessions.continuation_value_batch_tail import retain_value_batch_tail_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composition_rejection_events_table as rejections
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.sessions import test_continuation_live_phases as phases
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case

operation_store = phases.operation_store


def _forbid_execution(monkeypatch):
    entered = []

    def forbidden(*args, **kwargs):
        entered.append((args, kwargs))
        raise AssertionError("value batch entered external generic tool dispatch")

    async def provider(*args, **kwargs):
        entered.append((args, kwargs))
        raise AssertionError("value batch repeated the provider")

    monkeypatch.setattr(tool_batch, "execute_tool", forbidden)
    monkeypatch.setattr(litellm, "acompletion", provider)
    return entered


async def _prepare(owner, running):
    selected, inventory = await phases._finish_inventory(owner, running)
    runtime, actual_inventory = await owner.current(selected)
    assert actual_inventory.running is inventory.running
    return await preparation.prepare_value_batch(owner, runtime=runtime, selected=selected, inventory=actual_inventory)


def _write(store, owner, running, batch):
    service = owner.service._sessions_service
    assert type(service) is SessionServiceImpl
    with locked_session_transaction(store[0], str(store[4])) as conn, service._session_write_lock(conn, str(store[4])):
        return retain_value_batch_tail_on_connection(conn, running, service=service, batch=batch, published_at=datetime.now(UTC))


def _paid_rows(store):
    with store[0].connect() as conn:
        return tuple(tuple(conn.execute(select(table).where(table.c.session_id == str(store[4]))).all()) for table in (physical, ledger))


@pytest.mark.asyncio
async def test_original_two_value_calls_publish_in_order_and_cold_never_reexecutes(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(
        operation_store, tmp_path, monkeypatch, following_tool_call=("second_original", "set_metadata", {"patch": {"name": "second value"}})
    ) as (store, owner, running, sdk):
        external = _forbid_execution(monkeypatch)
        paid_before = _paid_rows(store)
        with store[0].connect() as conn:
            states_before = conn.execute(select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version)).all()
        rows_before_prepare = stored_rows(store[0])
        assert owner.runtime is not None
        original_runtime_state = owner.runtime.live.state.to_dict()
        computations = []
        actual_compute = computation._compute

        async def compute(call):
            computations.append(call.call.tool_call.id)
            return await actual_compute(call)

        monkeypatch.setattr(computation, "_compute", compute)
        batch = await _prepare(owner, running)
        assert computations == ["original_v4_tool", "second_original"]
        assert stored_rows(store[0]) == rows_before_prepare
        assert owner.runtime.live.state.to_dict() == original_runtime_state
        assert tuple(step.index for step in batch.steps) == (0, 1)
        assert tuple(call.id for call in batch.requested_calls) == ("original_v4_tool", "second_original")
        before_write = stored_rows(store[0])
        with pytest.raises(AuditIntegrityError, match="original complete preparation"):
            _write(store, owner, running, replace(batch))
        assert stored_rows(store[0]) == before_write
        stored = _write(store, owner, running, batch)
        with store[0].connect() as conn:
            states_after = conn.execute(select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version)).all()
        assert len(states_after) == len(states_before) + 2
        assert [row.metadata_["data"]["name"] for row in states_after[-2:]] == ["V4 original model metadata", "second value"]
        assert states_after[-1].derived_from_state_id == states_after[-2].id
        assert states_after[-1].version == states_after[-2].version + 1

        async def forbidden(*args, **kwargs):
            raise AssertionError("cold value batch repeated actual detached preparation")

        monkeypatch.setattr(preparation, "prepare_value_batch", forbidden)
        cold = ContinuationLivePhases(
            owner.service, owner.authority, owner.work, ProviderInvocationOwner(service=store[3], required_work=owner.work), owner.lease
        )
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=stored.snapshot, required_work=owner.work
        )
        rows_before_cold = stored_rows(store[0])
        runtime, _ = await cold.current(selected)
        assert runtime.live.state.metadata.name == "second value"
        assert runtime.graph.batch is not None
        assert tuple(outcome.call.id for outcome in runtime.graph.batch.batch.tool_outcomes) == tuple(
            call.id for call in batch.requested_calls
        )
        assert stored_rows(store[0]) == rows_before_cold and _paid_rows(store) == paid_before
        assert sdk and len(sdk) == 1 and not external
        assert computations == ["original_v4_tool", "second_original"]
        with locked_session_transaction(store[0], str(store[4])) as conn:
            verified = read_value_batch_application_on_connection(
                conn, running, expected=stored.snapshot, env_ref_names=batch.inventory.names
            )
            assert len(verified.tool_effect_keys) == 2 and verified.snapshot == stored.snapshot
            conn.execute(
                update(effects)
                .where(effects.c.session_id == str(store[4]), effects.c.effect_key == verified.tool_effect_keys[1])
                .values(input_sha256="0" * 64)
            )
            with pytest.raises(AuditIntegrityError):
                read_value_batch_application_on_connection(conn, running, expected=stored.snapshot, env_ref_names=batch.inventory.names)


@pytest.mark.asyncio
async def test_value_batch_publication_failure_rolls_back_every_original_prefix(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(
        operation_store, tmp_path, monkeypatch, following_tool_call=("second_original", "set_metadata", {"patch": {"name": "second value"}})
    ) as (store, owner, running, _sdk):
        batch = await _prepare(owner, running)
        before, paid_before = stored_rows(store[0]), _paid_rows(store)
        original = SessionServiceImpl._insert_chat_message
        entered = []

        def fail(self, *args, **kwargs):
            entered.append(kwargs["role"])
            if len(entered) == 2:
                raise RuntimeError("actual second value-batch publication insert failed")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(SessionServiceImpl, "_insert_chat_message", fail)
        with pytest.raises(RuntimeError, match="actual second"):
            _write(store, owner, running, batch)
        assert entered == ["assistant", "tool"]
        assert stored_rows(store[0]) == before and _paid_rows(store) == paid_before


@pytest.mark.asyncio
async def test_value_batch_actual_preparation_error_has_no_dispatch_and_rejects_changed_source(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(
        operation_store,
        tmp_path,
        monkeypatch,
        arguments={"patch": {"name": ["invalid name type"]}},
        following_tool_call=("second_original", "set_metadata", {"patch": {"name": "following valid value"}}),
    ) as (store, owner, running, sdk):
        external = _forbid_execution(monkeypatch)
        batch = await _prepare(owner, running)
        assert batch.steps[0].issued is None and batch.steps[0].memory_completion is None
        assert batch.steps[1].issued is not None
        original_plan = tail.plan_value_batch_publication_on_connection
        planned = []

        def check_plan(*args, **kwargs):
            publication = original_plan(*args, **kwargs)
            publication.require_original()
            assert len(publication.rejections) == 1
            changed = replace(publication.rejections[0], id=uuid4())
            with pytest.raises(AuditIntegrityError, match="original source"):
                replace(publication, rejections=(changed,)).require_original()
            with pytest.raises(AuditIntegrityError, match="original source"):
                replace(publication, rejection_ids=(uuid4(),)).require_original()
            planned.append(publication)
            return publication

        monkeypatch.setattr(tail, "plan_value_batch_publication_on_connection", check_plan)
        stored = _write(store, owner, running, batch)
        assert len(planned) == 1
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=stored.snapshot, required_work=owner.work
        )
        cold_owner = ContinuationLivePhases(
            owner.service, owner.authority, owner.work, ProviderInvocationOwner(service=store[3], required_work=owner.work), owner.lease
        )
        restored, _ = await cold_owner.current(selected)
        assert restored.graph.batch is not None
        assert restored.graph.batch.batch.tool_outcomes[0].error_category is not None
        assert restored.live.state.metadata.name == "following valid value"
        with store[0].begin() as conn:
            rejection = conn.execute(select(rejections).where(rejections.c.session_id == str(store[4]))).one()
            assert rejection.id == str(planned[0].rejection_ids[0])
            row = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(store[4]), effects.c.owner == "tool_dispatch", effects.c.disposition == "refused"
                )
            ).one()
            args = {
                "session_id": store[4],
                "operation_id": running.claim.operation_id,
                "effect_key": row.effect_key,
                "env_ref_names": batch.inventory.names,
            }
            proof = read_value_batch_preparation_refusal_on_connection(conn, **args)
            assert proof.tool_index == 0 and proof.tool_call_id == "original_v4_tool"
            conn.execute(
                update(effects)
                .where(effects.c.session_id == str(store[4]), effects.c.effect_key == row.effect_key)
                .values(input_sha256="0" * 64)
            )
            with pytest.raises(AuditIntegrityError, match="physical identity"):
                read_value_batch_preparation_refusal_on_connection(conn, **args)
        assert len(sdk) == 1 and not external


@pytest.mark.asyncio
@pytest.mark.parametrize("short", (False, True), ids=("exact-fit", "one-byte-short"))
async def test_value_batch_reserves_all_prefixes_and_final_floor_before_first_issue(operation_store, tmp_path, monkeypatch, short):
    async with _v4_tool_case(
        operation_store, tmp_path, monkeypatch, following_tool_call=("second_original", "set_metadata", {"patch": {"name": "second value"}})
    ) as (store, owner, running, _sdk):
        batch = await _prepare(owner, running)
        policy = decode_continuation_policy(batch.selected.stack.admission_policy_json, batch.selected.stack.policy_hash)
        before = stored_rows(store[0])
        actual_scope, actual_consume = tail.admit_prepared_complete_tail_on_connection, tail.consume_value_batch_transitions_on_connection
        entered, measured = [], []

        def consume(*args, **kwargs):
            entered.append(kwargs["plan"])
            return actual_consume(*args, **kwargs)

        @contextmanager
        def at_capacity(conn, *, original, prefixes):
            assert len(prefixes) == 7
            sid, operation = str(store[4]), running.claim.operation_id
            complete = {
                row.chunk_key: bytes(row.payload)
                for row in conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation))
            }
            last = prefixes[-1].append
            floor = continuation_unpaid_terminal_reserve_on_connection(
                conn,
                session_id=sid,
                operation_id=operation,
                post_root_json=last.snapshot_json,
                post_cursor=last.expected_cursor + len(last.events),
            )
            for prefix in prefixes:
                for chunk in (*prefix.chunks, *prepared_append_chunks(prefix.append)):
                    if chunk.key in complete:
                        assert complete[chunk.key] == chunk.payload
                    complete[chunk.key] = chunk.payload
            for chunk in floor:
                if chunk.key in complete:
                    assert complete[chunk.key] == chunk.payload
                complete[chunk.key] = chunk.payload
            demand = sum(len(payload) for payload in complete.values())
            ballast = policy.max_retained_payload_bytes - demand + int(short)
            assert ballast > 0
            for index, offset in enumerate(range(0, ballast, 65_536)):
                payload = b"b" * min(65_536, ballast - offset)
                conn.execute(
                    insert(chunks).values(
                        session_id=sid,
                        operation_id=operation,
                        chunk_key=f"value-capacity-control:{index}",
                        codec="composer.capacity-control.v1",
                        payload=payload,
                        sha256=sha256(payload).hexdigest(),
                        byte_length=len(payload),
                    )
                )
            measured.append(demand + ballast)
            with actual_scope(conn, original=original, prefixes=prefixes):
                yield

        monkeypatch.setattr(tail, "admit_prepared_complete_tail_on_connection", at_capacity)
        monkeypatch.setattr(tail, "consume_value_batch_transitions_on_connection", consume)
        if short:
            with pytest.raises(ContinuationRetainedCapacityExceeded):
                _write(store, owner, running, batch)
            assert not entered and stored_rows(store[0]) == before
        else:
            _write(store, owner, running, batch)
            assert len(entered) == 1
        assert measured == [policy.max_retained_payload_bytes + int(short)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("creator", "call_index"))
async def test_p4_rejects_forged_v4_preparation_refusal_before_publication(operation_store, tmp_path, monkeypatch, change):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.sessions.continuation_value_batch_refusal import ValueBatchPreparationRefusal

    async with _v4_tool_case(
        operation_store,
        tmp_path,
        monkeypatch,
        arguments={"patch": {"name": ["invalid name type"]}},
        following_tool_call=("second_original", "set_metadata", {"patch": {"name": "following valid value"}}),
    ) as (store, owner, running, _sdk):
        batch = await _prepare(owner, running)
        assert batch.steps[0].issued is None
        original = tail.prepare_p4_on_connection
        entered = []

        def forge(conn, *args, **kwargs):
            row = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(store[4]), effects.c.owner == "tool_dispatch", effects.c.disposition == "refused"
                )
            ).one()
            source = ValueBatchPreparationRefusal.model_validate_json(row.action_json, strict=True)
            changed = source.model_copy(
                update={"creator_operation_epoch": source.creator_operation_epoch + 1}
                if change == "creator"
                else {"tool_index": source.tool_index + 1}
            )
            conn.execute(
                update(effects)
                .where(effects.c.session_id == str(store[4]), effects.c.effect_key == row.effect_key)
                .values(action_json=canonical_json(changed.model_dump(mode="json")))
            )
            entered.append(change)
            return original(conn, *args, **kwargs)

        monkeypatch.setattr(tail, "prepare_p4_on_connection", forge)
        before = stored_rows(store[0])
        with pytest.raises(AuditIntegrityError, match="value preparation refusal"):
            _write(store, owner, running, batch)
        assert entered == [change] and stored_rows(store[0]) == before


@pytest.mark.asyncio
async def test_value_batch_rejection_row_tamper_cannot_change_preissued_p4_receipt(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(
        operation_store,
        tmp_path,
        monkeypatch,
        arguments={"patch": {"name": ["invalid name type"]}},
        following_tool_call=("second_original", "set_metadata", {"patch": {"name": "following valid value"}}),
    ) as (store, owner, running, _sdk):
        batch = await _prepare(owner, running)
        before = stored_rows(store[0])
        original = tail.consume_value_batch_publication_on_connection
        entered = []

        def corrupt(conn, *args, **kwargs):
            result = original(conn, *args, **kwargs)
            row = conn.execute(select(rejections).where(rejections.c.session_id == str(store[4]))).one()
            conn.execute(update(rejections).where(rejections.c.id == row.id).values(message="changed original rejection"))
            entered.append(row.id)
            return result

        monkeypatch.setattr(tail, "consume_value_batch_publication_on_connection", corrupt)
        with pytest.raises(AuditIntegrityError, match="rejection_hashes"):
            _write(store, owner, running, batch)
        assert len(entered) == 1 and stored_rows(store[0]) == before
        with store[0].connect() as conn:
            assert conn.execute(select(rejections).where(rejections.c.session_id == str(store[4]))).first() is None
