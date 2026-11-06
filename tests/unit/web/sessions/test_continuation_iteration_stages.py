"""Actual P4 custody and opposing controls for post-batch paid stage cuts."""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions import continuation_iteration_return as stages
from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.unit.web.sessions import test_continuation_live_phases as live_fixture
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = live_fixture.operation_store


@asynccontextmanager
async def _applied_case(store, tmp_path, monkeypatch):
    async with live_fixture._phase_case(store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        for _ in range(len(primary.outcome.completion.tool_batch.calls) + 1):
            await live_fixture._advance(owner)
        assert owner.p4 is not None and type(owner.p4.application) is StoredP4Application
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.p4.application.snapshot, required_work=owner.work
        )
        runtime, inventory = await owner.current(selected)
        assert owner.finished_batch is not None and len(sdk) == 1
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        yield owner, running, selected, inventory, captured, sdk
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_p4_preparation_and_classifier_begin_are_separate_no_sdk_cuts(operation_store, tmp_path, monkeypatch):
    async with _applied_case(operation_store, tmp_path, monkeypatch) as (owner, running, selected, inventory, captured, sdk):
        prepared = stages.retain_iteration_prepared(owner.authority, running, selected=selected, inventory=inventory, captured=captured)
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            stage = stages.read_iteration_stage_on_connection(conn, running, selected=prepared)
            assert stage is not None and stage.stage == "classify_ready" and not stage.early_required
            row = conn.execute(select(effects).where(effects.c.effect_key == stage.owner_effect_key)).one_or_none()
            assert row is None
        assert prepared.cursor == selected.stack.cursor + 1 and len(sdk) == 1
        following = await load_selected_continuation(
            authority=owner.authority, running=running, selected=prepared, required_work=owner.work
        )
        _runtime, current_inventory = await owner.current(following)
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="ready source"):
            stages.begin_iteration_paid_stage(owner.authority, running, selected=following, inventory=current_inventory, stage="early")
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        begun = stages.begin_iteration_paid_stage(
            owner.authority, running, selected=following, inventory=current_inventory, stage="classify"
        )
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            actual = stages.read_iteration_stage_on_connection(conn, running, selected=begun)
        assert actual is not None and actual.stage == "classify_dispatched"
        assert actual.owner_effect_key == stage.owner_effect_key and begun.cursor == prepared.cursor + 1 and len(sdk) == 1


@pytest.mark.asyncio
async def test_unversioned_dispatched_post_batch_owner_never_becomes_a_ready_stage(operation_store, tmp_path, monkeypatch):
    async with _applied_case(operation_store, tmp_path, monkeypatch) as (owner, running, selected, _inventory, _captured, sdk):
        original = await live_fixture.post_batch._retain(owner, selected, "iteration_review_dispatched")
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            from elspeth.web.sessions.continuation import load_snapshot

            stored = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            before = stored_rows(operation_store[0])
            with pytest.raises(AuditIntegrityError, match="reconciliation, never paid replay"):
                stages.read_iteration_stage_on_connection(conn, running, selected=stored)
            assert stored.checkpoint_id == original.selected.checkpoint_id
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("owner", "input_sha256", "result_chunk_key"))
async def test_ready_stage_reproves_original_classifier_effect_before_paid_work(operation_store, tmp_path, monkeypatch, change):
    async with _applied_case(operation_store, tmp_path, monkeypatch) as (owner, running, selected, inventory, captured, sdk):
        prepared = stages.retain_iteration_prepared(owner.authority, running, selected=selected, inventory=inventory, captured=captured)
        following = await load_selected_continuation(
            authority=owner.authority, running=running, selected=prepared, required_work=owner.work
        )
        _runtime, current_inventory = await owner.current(following)
        begun = stages.begin_iteration_paid_stage(
            owner.authority, running, selected=following, inventory=current_inventory, stage="classify"
        )
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            stage = stages.read_iteration_stage_on_connection(conn, running, selected=begun)
            assert stage is not None
            conn.execute(
                update(effects)
                .where(effects.c.effect_key == stage.owner_effect_key)
                .values(
                    **(
                        {"owner": "session_sql"}
                        if change == "owner"
                        else {"input_sha256": "f" * 64}
                        if change == "input_sha256"
                        else {"result_chunk_key": f"snapshot:{prepared.checkpoint_id}"}
                    )
                )
            )
        before = stored_rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn,
            pytest.raises(AuditIntegrityError, match=r"original classifier effect|uncompleted classifier"),
        ):
            stages.read_iteration_stage_on_connection(conn, running, selected=begun)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_coherent_dispatch_record_cannot_invent_capture_or_paid_counters(operation_store, tmp_path, monkeypatch):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, put_chunk

    async with _applied_case(operation_store, tmp_path, monkeypatch) as (owner, running, selected, inventory, captured, sdk):
        prepared = stages.retain_iteration_prepared(owner.authority, running, selected=selected, inventory=inventory, captured=captured)
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(operation_store[0], sid) as conn:
            actual = stages._stage_record(conn, prepared, sid, operation)
            assert actual is not None
            prior = actual[0]
            forged = prior.model_copy(
                update={
                    "before_checkpoint_id": prepared.checkpoint_id,
                    "before_cursor": prepared.cursor,
                    "stage": "classify_dispatched",
                    "compose": prior.compose.model_copy(update={"advisor_calls": prior.compose.advisor_calls + 1}),
                }
            )
            canonical = canonical_json(forged.model_dump(mode="json"))
            key = stages._key(canonical)
            put_chunk(conn, session_id=sid, operation_id=operation, key=key, payload=canonical.encode("utf-8"), codec=forged.schema_version)
            root = decode_continuation_snapshot(prepared.payload_json)
            main = stages.advance_effect_key(forged.batch.response)
            source_batch = stages._source_batch(conn, forged)
            conn.execute(
                insert(effects).values(
                    session_id=sid,
                    operation_id=operation,
                    effect_key=main,
                    owner=stages.ITERATION_EFFECT_OWNER,
                    disposition="dispatched",
                    input_sha256=stages.stable_hash({"schema": "composer.post-batch.v1", "batch": source_batch.accumulator.digest}),
                    creator_sequence=forged.creator_sequence,
                    creator_generation=forged.creator_generation,
                )
            )
            changed = root.model_copy(
                update={"frames": (forged.compose, forged.batch), "effect_frontier": stages._frontier(root, forged, key)}
            )
            planted = append_complete_snapshot(
                conn,
                session_id=sid,
                operation_id=operation,
                expected_checkpoint_id=prepared.checkpoint_id,
                expected_cursor=prepared.cursor,
                snapshot_json=encode_continuation_snapshot(changed),
                event_batch=(ContinuationEvent("checkpoint", stages._stage_event(forged, key)),),
                handoff=False,
            )
        before = stored_rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], sid) as conn,
            pytest.raises(AuditIntegrityError, match="dispatch cannot change"),
        ):
            stages.read_iteration_stage_on_connection(conn, running, selected=planted)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_cold_capture_reproves_actual_graph_instead_of_accepting_an_old_same_codec_chunk(operation_store, tmp_path, monkeypatch):
    from dataclasses import replace

    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot

    async with _applied_case(operation_store, tmp_path, monkeypatch) as (owner, running, selected, inventory, captured, sdk):
        prepared = stages.retain_iteration_prepared(owner.authority, running, selected=selected, inventory=inventory, captured=captured)
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(operation_store[0], sid) as conn:
            actual = stages._stage_record(conn, prepared, sid, operation)
            assert actual is not None
            record, key = actual
            stages._prove_record(conn, prepared, record, key)
            original_batch = stages.applied_batch_cut(selected.stack.frames)[1]
            assert original_batch.accumulator != record.batch.accumulator
            assert original_batch.accumulator.codec == record.batch.accumulator.codec
            substituted = record.model_copy(update={"batch": record.batch.model_copy(update={"accumulator": original_batch.accumulator})})
            root = decode_continuation_snapshot(prepared.payload_json)
            coherent = replace(
                prepared, payload_json=encode_continuation_snapshot(root.model_copy(update={"frames": (record.compose, substituted.batch)}))
            )
            changed_key = stages._key(canonical_json(substituted.model_dump(mode="json")))
            with pytest.raises(AuditIntegrityError, match="complete shared evidence graph"):
                stages._prove_record(conn, coherent, substituted, changed_key)
        assert len(sdk) == 1
