"""One physical capture reuses pure work while each current SQL proof remains real."""

import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from threading import Event

import pytest
from sqlalchemy import delete, select, text, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_runtime as runtime_owner
from elspeth.web.composer.continuation_codec import (
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_mandatory_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.continuation_retention_memo import (
    RetentionStageMemo,
    current_retention_stage_memo,
    retention_stage_memo_scope,
)
from elspeth.web.sessions import continuation_runtime as sql_owner
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from tests.unit.web.coordination.test_continuation_p4_recovery_start import recovery_rows
from tests.unit.web.sessions import test_continuation_runtime as runtime_cases

operation_store = runtime_cases.operation_store


async def _capture(runtime, arguments):
    return await runtime_owner.capture_continuation_runtime_owners(
        runtime, selected=arguments["selected"], inventory=arguments["inventory"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["returned", "initial-read-error", "capture-error", "final-read-error", "started-cancellation"])
async def test_physical_capture_shares_one_fresh_memo_across_both_sql_proofs_and_closes_it(operation_store, monkeypatch, outcome):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    runtime = await runtime_owner.restore_continuation_runtime(**arguments)
    # A complete equal selection is valid DATA and exercises the existing full
    # initial acquisition rather than the same-object env-only optimization.
    arguments = {**arguments, "selected": replace(arguments["selected"])}
    original_read, original_capture = sql_owner.read_runtime_source_on_connection, runtime_owner._capture_owners
    sql_reads, captures, lookups, computations = [], [], [], []
    original_lookup = RetentionStageMemo._lookup
    entered, release = Event(), Event()
    original_error = ValueError("original physical capture error")

    def read(conn, *args, **kwargs):
        memo = current_retention_stage_memo()
        assert type(memo) is RetentionStageMemo and memo.active
        sql_reads.append((memo, conn))
        if outcome == "initial-read-error" or (outcome == "final-read-error" and len(sql_reads) == 2):
            raise original_error
        return original_read(conn, *args, **kwargs)

    def lookup(memo, entries, key, compute):
        lookups.append(memo)

        def actual_compute():
            computations.append(memo)
            return compute()

        return original_lookup(memo, entries, key, actual_compute)

    def capture(*args, **kwargs):
        memo = current_retention_stage_memo()
        assert type(memo) is RetentionStageMemo and memo.active
        captures.append(memo)
        result = original_capture(*args, **kwargs)
        assert memo.cached_entry_count > 0
        if outcome == "capture-error":
            raise original_error
        if outcome == "started-cancellation":
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
    monkeypatch.setattr(runtime_owner, "_capture_owners", capture)
    monkeypatch.setattr(RetentionStageMemo, "_lookup", lookup)
    before = recovery_rows(operation_store[0])
    try:
        with retention_stage_memo_scope() as outer:
            if outcome.endswith("error"):
                with pytest.raises(ValueError) as caught:
                    await _capture(runtime, arguments)
                assert caught.value is original_error
            elif outcome == "started-cancellation":
                task = asyncio.create_task(_capture(runtime, arguments))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    task.cancel("actual physical capture cancellation")
                    await asyncio.sleep(0)
                    assert not task.done() and captures[-1].active
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError, match="actual physical capture cancellation"):
                    await task
            else:
                first = await _capture(runtime, arguments)
                second = await _capture(runtime, arguments)
                assert first.selected is second.selected is arguments["selected"]
                assert len(captures) == 2 and captures[0] is not captures[1]
                for memo in captures:
                    assert sum(actual is memo for actual in lookups) > sum(actual is memo for actual in computations) > 0
            assert current_retention_stage_memo() is outer and outer.active
            assert all(memo is not outer and not memo.active and memo.cached_entry_count == 0 for memo, _conn in sql_reads)
        if outcome == "initial-read-error":
            assert captures == [] and len(sql_reads) == 1
            expected = [sql_reads[0][0]]
        else:
            expected = [memo for memo in captures for _ in range(1 if outcome == "capture-error" else 2)]
        assert len(sql_reads) == len(expected)
        assert all(actual is wanted for (actual, _conn), wanted in zip(sql_reads, expected, strict=True))
        assert all(conn.closed for _memo, conn in sql_reads)
        if outcome not in ("initial-read-error", "capture-error"):
            assert sql_reads[0][1] is not sql_reads[1][1]
        assert current_retention_stage_memo() is None
        assert not outer.active and outer.cached_entry_count == 0
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        release.set()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_runtime_facade_defaults_to_fresh_isolation_and_borrows_only_explicit_current_memo(operation_store, monkeypatch):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    original_read = sql_owner.read_runtime_source_on_connection
    authority = arguments["authority"]
    observed = []

    def read(*args, **kwargs):
        observed.append(current_retention_stage_memo())
        return original_read(*args, **kwargs)

    monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
    try:
        with retention_stage_memo_scope() as outer:
            for _ in range(2):
                await asyncio.to_thread(
                    authority.read_continuation_runtime,
                    running,
                    selected=arguments["selected"],
                    inventory=arguments["inventory"],
                )
                assert observed[-1] is not outer and not observed[-1].active
            assert observed[0] is not observed[1]
            await asyncio.to_thread(
                authority.read_continuation_runtime,
                running,
                selected=arguments["selected"],
                inventory=arguments["inventory"],
                retention_memo=outer,
            )
            assert observed[-1] is outer and outer.active
        assert not outer.active and outer.cached_entry_count == 0
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["foreign", "closed", "wrong-current"])
async def test_runtime_facade_refuses_invalid_borrow_before_sql(operation_store, monkeypatch, kind):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    original_read = sql_owner.read_runtime_source_on_connection
    authority = arguments["authority"]
    observed = []

    def read(*args, **kwargs):
        observed.append(True)
        return original_read(*args, **kwargs)

    monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
    with retention_stage_memo_scope() as closed:
        pass
    try:
        with retention_stage_memo_scope() as outer, retention_stage_memo_scope() as current:
            borrowed = object() if kind == "foreign" else closed if kind == "closed" else outer
            before = recovery_rows(operation_store[0])
            with pytest.raises(AuditIntegrityError, match="exact active retention memo"):
                await asyncio.to_thread(
                    authority.read_continuation_runtime,
                    running,
                    selected=arguments["selected"],
                    inventory=arguments["inventory"],
                    retention_memo=borrowed,
                )
            assert observed == [] and recovery_rows(operation_store[0]) == before
            assert current_retention_stage_memo() is current and current.active and outer.active
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["sql-bytes", "sql-missing", "sql-storage-type", "current-policy", "current-inventory"])
async def test_capture_memo_does_not_replace_final_current_sql_validation(operation_store, monkeypatch, change):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    runtime = await runtime_owner.restore_continuation_runtime(**arguments)
    await _capture(runtime, arguments)
    arguments = {**arguments, "selected": replace(arguments["selected"])}
    original_read, original_capture = sql_owner.read_runtime_source_on_connection, runtime_owner._capture_owners
    reads, capture_memos, after_mutation = [], [], []

    def read(*args, **kwargs):
        reads.append(current_retention_stage_memo())
        return original_read(*args, **kwargs)

    def capture(*args, **kwargs):
        capture_memos.append(current_retention_stage_memo())
        result = original_capture(*args, **kwargs)
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            if change in ("sql-bytes", "sql-missing", "sql-storage-type"):
                reference = arguments["selected"].stack.frames[0].caches
                original = next(chunk for chunk in arguments["selected"].chunks if chunk.reference == reference)
                predicate = (
                    (chunks.c.session_id == str(running.claim.session_id))
                    & (chunks.c.operation_id == running.claim.operation_id)
                    & (chunks.c.chunk_key == private_chunk_key(original))
                )
                if change == "sql-missing":
                    changed = conn.execute(delete(chunks).where(predicate))
                elif change == "sql-storage-type":
                    assert conn.dialect.name == "sqlite" and original.canonical.isascii()
                    # Keep actual bytes/length/SHA equal while changing SQLite's
                    # storage class to TEXT; no constraint is disabled.
                    changed = conn.execute(
                        text(
                            "UPDATE composer_continuation_chunks SET payload=:payload "
                            "WHERE session_id=:sid AND operation_id=:oid AND chunk_key=:key"
                        ),
                        {
                            "payload": original.canonical,
                            "sid": str(running.claim.session_id),
                            "oid": running.claim.operation_id,
                            "key": private_chunk_key(original),
                        },
                    )
                else:
                    payload = original.canonical.encode("utf-8") + b" "
                    changed = conn.execute(
                        update(chunks)
                        .where(predicate)
                        .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                    )
                assert changed.rowcount == 1
            elif change == "current-policy":
                different = "0" * 64 if arguments["selected"].stack.policy_hash != "0" * 64 else "1" * 64
                changed = conn.execute(
                    update(segments)
                    .where(
                        segments.c.session_id == str(running.claim.session_id),
                        segments.c.operation_id == running.claim.operation_id,
                        segments.c.sequence == running.claim.segment_sequence,
                        segments.c.generation == running.claim.ownership_generation,
                    )
                    .values(policy_sha256=different)
                )
                assert changed.rowcount == 1
            else:
                for chunk in result.chunks:
                    put_chunk(
                        conn,
                        session_id=str(running.claim.session_id),
                        operation_id=running.claim.operation_id,
                        key=private_chunk_key(chunk),
                        payload=chunk.canonical.encode("utf-8"),
                        codec=chunk.reference.codec,
                    )
                append_complete_snapshot(
                    conn,
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    expected_checkpoint_id=result.selected.checkpoint_id,
                    expected_cursor=result.selected.stack.cursor,
                    snapshot_json=encode_continuation_snapshot(result.stack),
                    event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"actual capture changes current selection"}'),),
                    handoff=False,
                )
        after_mutation.append(recovery_rows(operation_store[0]))
        return result

    monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
    monkeypatch.setattr(runtime_owner, "_capture_owners", capture)
    try:
        with pytest.raises(AuditIntegrityError):
            await _capture(runtime, arguments)
        assert len(reads) == 2 and len(capture_memos) == 1 and len(after_mutation) == 1
        assert reads[0] is reads[1] is capture_memos[0]
        assert not reads[0].active and reads[0].cached_entry_count == 0
        assert recovery_rows(operation_store[0]) == after_mutation[0]
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_final_capture_rechecks_nonselected_historical_witness(operation_store, tmp_path, monkeypatch):
    from tests.unit.web.sessions import test_continuation_live_phases as phases
    from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case

    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        await phases._advance(owner)
        assert owner.latest_snapshot is not None
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        current = await owner.restore_finished_batch(selected)
        runtime, inventory = current.runtime, current.inventory
        arguments = {"selected": selected, "inventory": inventory}
        await _capture(runtime, arguments)
        assert len(sdk) == 1
        actual_capture = runtime_owner._capture_owners
        actual_read = sql_owner.read_runtime_source_on_connection
        mutated, final_errors = [], []

        def capture(*args, **values):
            result = actual_capture(*args, **values)
            source = values["source"]
            assert source.evidence.application_entry is not None
            record = source.evidence.application_entry.applications[0]
            with store[0].begin() as conn:
                old = conn.execute(select(checkpoints).where(checkpoints.c.checkpoint_id == record.checkpoint_id)).one()
                assert old.snapshot_chunk_key not in {private_chunk_key(chunk) for chunk in source.selected.chunks}
                chunk = conn.execute(
                    select(chunks).where(chunks.c.session_id == str(store[4]), chunks.c.chunk_key == old.snapshot_chunk_key)
                ).one()
                root = decode_continuation_snapshot(chunk.payload.decode("utf-8"))
                top = root.frames[-1]
                changed_top = top.model_copy(update={"next_tool_index": 1 if top.next_tool_index == 0 else 0})
                changed_root = root.model_copy(update={"frames": (*root.frames[:-1], changed_top)})
                payload = encode_continuation_snapshot(changed_root).encode("utf-8")
                assert decode_continuation_snapshot(payload.decode("utf-8")) == changed_root
                changed = conn.execute(
                    update(chunks)
                    .where(chunks.c.session_id == str(store[4]), chunks.c.chunk_key == old.snapshot_chunk_key)
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
                assert changed.rowcount == 1
            mutated.append((source, recovery_rows(store[0])))
            return result

        def read(*args, **values):
            assert values["selected"] is mutated[-1][0].selected
            try:
                return actual_read(*args, **values)
            except AuditIntegrityError as error:
                final_errors.append(error)
                raise

        monkeypatch.setattr(runtime_owner, "_capture_owners", capture)
        monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
        with pytest.raises(AuditIntegrityError) as refused:
            await _capture(runtime, arguments)
        assert len(mutated) == 1 and final_errors == [refused.value]
        assert recovery_rows(store[0]) == mutated[0][1] and len(sdk) == 1
        owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_failure", (False, True), ids=("original-refusal", "ordered-refusal-and-source-failure"))
async def test_final_capture_preserves_actual_retention_refusal_and_final_failure_group(operation_store, monkeypatch, final_failure):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    runtime = await runtime_owner.restore_continuation_runtime(**arguments)
    await _capture(runtime, arguments)
    actual_capture, actual_read = runtime_owner._capture_owners, sql_owner.read_runtime_source_on_connection
    refusals, failures, checked, after_mutation = [], [], [], []

    def capture(*args, **values):
        actual_capture(*args, **values)
        if final_failure:
            reference = arguments["selected"].stack.frames[0].caches
            with operation_store[0].begin() as conn:
                payload = b"{}"
                changed = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(running.claim.session_id),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                    )
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
                assert changed.rowcount == 1
        after_mutation.append(recovery_rows(operation_store[0]))
        # Exercise the real mandatory encoder after real owner capture. This
        # extra document is test DATA, never a replacement selected source.
        try:
            encode_mandatory_private_chunk(
                codec="composer.messages.v1", value={"padding": "x" * CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES}
            )
        except ComposerContextUnsupported as refused:
            refusals.append(refused)
            raise
        raise AssertionError("actual over-bound mandatory document was accepted")

    def read(*args, **values):
        checked.append(values["selected"])
        assert values["selected"] is arguments["selected"]
        try:
            return actual_read(*args, **values)
        except AuditIntegrityError as error:
            failures.append(error)
            raise

    monkeypatch.setattr(runtime_owner, "_capture_owners", capture)
    monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
    try:
        if final_failure:
            with pytest.raises(BaseExceptionGroup, match="Retention refusal and final current-source admission") as grouped:
                await _capture(runtime, arguments)
            assert len(refusals) == len(failures) == 1
            assert grouped.value.exceptions == (refusals[0], failures[0])
        else:
            with pytest.raises(ComposerContextUnsupported) as carried:
                await _capture(runtime, arguments)
            assert len(refusals) == 1 and not failures
            assert carried.value.__cause__ is refusals[0] and carried.value.reason == refusals[0].reason == "retained_chunk_bytes"
            assert carried.value.tool_invocations == runtime.live.recorder.invocations[runtime.drained.invocations :]
            assert carried.value.llm_calls == runtime.live.recorder.llm_calls[runtime.drained.llm_calls :]
        assert checked == [arguments["selected"]] and len(after_mutation) == 1
        assert recovery_rows(operation_store[0]) == after_mutation[0]
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", (False, True), ids=("cold", "warm"))
async def test_capture_prefix_uses_original_roundtripped_chunk_and_one_fresh_live_encoding(operation_store, monkeypatch, warm):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    runtime = await runtime_owner.restore_continuation_runtime(**arguments)
    arguments = {**arguments, "selected": replace(arguments["selected"])}
    original_read = sql_owner.read_runtime_source_on_connection
    original_decode = runtime_owner.decode_recorder_snapshot
    original_encode = runtime_owner.encode_recorder_snapshot
    reads, decoded, encoded = [], [], []

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        reads.append(result)
        return result

    def decode(chunk):
        result = original_decode(chunk)
        decoded.append((chunk, result))
        return result

    def encode(snapshot):
        result = original_encode(snapshot)
        encoded.append(result)
        return result

    try:
        if warm:
            await _capture(runtime, arguments)
        original_prefix = runtime.live.recorder.withheld_replies
        assert original_prefix
        runtime.live.recorder.record_withheld_reply("repair_gate_superseded", "Permitted live append after the retained prefix")
        monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
        monkeypatch.setattr(runtime_owner, "decode_recorder_snapshot", decode)
        monkeypatch.setattr(runtime_owner, "encode_recorder_snapshot", encode)
        result = await _capture(runtime, arguments)
        # The original source and final SQL proof both use the full reader.
        assert len(reads) == 2
        assert len(decoded) == len(encoded) == 1
        assert decoded[0][0] is reads[0].admitted_evidence.recorder_chunk
        assert encoded[0] == decoded[0][0]
        frame = result.stack.frames[0]
        retained = original_decode(next(chunk for chunk in result.chunks if chunk.reference == frame.audit_buffer))
        assert retained.withheld_replies[:-1] == original_prefix
        assert retained.withheld_replies[-1].content == "Permitted live append after the retained prefix"
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("live-prefix", "source-bytes", "source-reference"))
async def test_capture_prefix_handoff_keeps_fresh_live_and_original_chunk_opponents(operation_store, monkeypatch, change):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    runtime = await runtime_owner.restore_continuation_runtime(**arguments)
    arguments = {**arguments, "selected": replace(arguments["selected"])}
    original_read = sql_owner.read_runtime_source_on_connection
    original_decode = runtime_owner.decode_recorder_snapshot
    original_encode = runtime_owner.encode_recorder_snapshot
    reads, decodes, encodes = [], [], []

    def read(*args, **kwargs):
        reads.append(True)
        return original_read(*args, **kwargs)

    def decode(chunk):
        decodes.append(True)
        if change == "source-bytes":
            document = json.loads(chunk.canonical)
            assert document["snapshot"]["withheld_replies"]
            document["snapshot"]["withheld_replies"][0]["content"] = "Changed original source payload"
            chunk = replace(chunk, canonical=canonical_json(document))
        elif change == "source-reference":
            chunk = replace(chunk, reference=chunk.reference.model_copy(update={"digest": "0" * 64}))
        return original_decode(chunk)

    def encode(snapshot):
        encodes.append(True)
        return original_encode(snapshot)

    try:
        if change == "live-prefix":
            assert runtime.live.recorder.withheld_replies
            original = runtime.live.recorder._withheld_replies[0]
            runtime.live.recorder._withheld_replies[0] = replace(original, content="Changed retained live prefix")
        monkeypatch.setattr(sql_owner, "read_runtime_source_on_connection", read)
        monkeypatch.setattr(runtime_owner, "decode_recorder_snapshot", decode)
        monkeypatch.setattr(runtime_owner, "encode_recorder_snapshot", encode)
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError) as refused:
            await _capture(runtime, arguments)
        assert reads == decodes == [True]
        if change == "live-prefix":
            assert encodes == [True] and "complete current SQL recorder prefix" in str(refused.value)
        else:
            assert encodes == [] and "bytes differ from their binding" in str(refused.value)
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
