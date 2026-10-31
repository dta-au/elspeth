"""Fresh mandatory-retention refusals preserve actual prior continuation work.

These cases use current SQL/runtime owners and the existing controlled external
transport. They do not install a replacement continuation driver.
"""

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_primary import read_continuation_primary
from elspeth.web.composer.continuation_runtime import _work_owners, capture_continuation_runtime_owners, restore_continuation_runtime
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.coordination.composer_operation_authority import settle_composer_operation_on_connection
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRecord
from elspeth.web.sessions.continuation_runtime import read_runtime_source_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_runtime_provider_history import (
    append_actual_native_and_capture,
    full_rows,
    prepared_runtime,
    publish,
)

operation_store = operations.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", (False, True), ids=("before-sdk", "after-paid-publication"))
async def test_fresh_runtime_shelf_excess_keeps_last_checkpoint_and_paid_evidence(operation_store, tmp_path, monkeypatch, paid):
    arguments, runtime, sdk_calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        result = None
        if paid:
            result = await publish(operation_store, arguments, runtime)
            arguments, runtime = await append_actual_native_and_capture(operation_store, arguments, runtime, result)
        else:
            await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        prior = arguments["selected"]
        before = full_rows(operation_store[0])
        original_messages = tuple(runtime.live.llm_messages)
        # Each ordinary message remains below the existing credential scalar
        # bound. The complete shelf duplicates its full observed event text;
        # its canonical escaping, rather than a single message, crosses 1 MiB.
        amendments = [{"role": "user", "content": f"Amendment {number}: " + 'é\\"\n' * 24_000} for number in range(3)]
        assert all(len(canonical_json(message).encode("utf-8")) < 262_144 for message in amendments)
        runtime.live.llm_messages.extend(amendments)
        runtime.live.turn_evidence.transcript.observe_messages(amendments)
        expected_transcript = runtime.live.turn_evidence.transcript.snapshot()
        with pytest.raises(ComposerContextUnsupported) as refused:
            await capture_continuation_runtime_owners(runtime, selected=prior, inventory=arguments["inventory"])
        assert refused.value.reason == "retained_history_bytes"
        assert refused.value.llm_calls == runtime.live.recorder.llm_calls[runtime.drained.llm_calls :]
        assert refused.value.tool_invocations == runtime.live.recorder.invocations[runtime.drained.invocations :]
        assert runtime.live.turn_evidence.transcript.snapshot() == expected_transcript
        assert tuple(runtime.live.llm_messages) == (*original_messages, *amendments)
        assert full_rows(operation_store[0]) == before
        assert len(sdk_calls) == int(paid)
        restored = await restore_continuation_runtime(**arguments)
        assert restored.source.selected == prior
        assert tuple(restored.live.llm_messages) == original_messages
        if result is not None:
            retained = await read_continuation_primary(
                authority=arguments["authority"], selected=prior, required_work=arguments["required_work"]
            )
            assert retained.response == result.response
            assert retained.outcome.audit_call == result.outcome.audit_call
            assert restored.live.recorder.llm_calls == runtime.live.recorder.llm_calls
        projected = project_composer_operation_error(refused.value, request_id="retention-request")
        assert projected.http_status == 422 and projected.error_type == "composer_context_unsupported"
        assert projected.body["detail"]["reason"] == "retained_history_bytes"
        integrity = AuditIntegrityError("required prior effect verification failed")
        grouped = project_composer_operation_error(ExceptionGroup("required evidence", [refused.value, integrity]), request_id=None)
        assert grouped.http_status == 500 and grouped.error_type == "audit_integrity_error"
        arguments["required_work"].coordinator.assert_completed()
        # Prove the V2 library terminal using the last valid stored cut under
        # its current lock/owner checks. The HTTP/worker join remains separate.
        work = arguments["required_work"]

        def settle_refusal():
            assert _work_owners(arguments["authority"], arguments["inventory"], work) is running
            with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
                current = read_runtime_source_on_connection(conn, running, selected=prior, inventory=arguments["inventory"])
                assert current.selected == prior
                assert current.bootstrap == runtime.source.bootstrap and current.original == runtime.source.original
                assert current.evidence == restored.source.evidence
                assert (current.timing.sequence, current.timing.generation) == (
                    running.claim.segment_sequence,
                    running.claim.ownership_generation,
                )
                assert all(effect.disposition not in ("dispatched", "unknown") for effect in current.selected.stack.effect_frontier)
                return settle_composer_operation_on_connection(conn, running, outcome=projected)

        terminal = await _registered(
            work=work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=settle_refusal,
            expected=ComposerOperationRecord,
        )
        assert terminal.status == "failed" and terminal.result_json is not None
        assert terminal.lifecycle_version == 2 and terminal.phase == "settled"
        assert (terminal.attempt, terminal.session_operation_id, terminal.session_operation_epoch) == (
            running.claim.attempt,
            running.session_operation_context.fence.operation_id,
            running.session_operation_context.fence.operation_epoch,
        )
        settled = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert settled == projected
        assert arguments["authority"].get(session_id=operation_store[4], operation_id=running.claim.operation_id) == terminal
        after = full_rows(operation_store[0])
        # Prior checkpoint, event, and chunk rows survive an appended terminal.
        for table_index in (4, 5, 7):
            assert all(row in after[0][table_index] for row in before[0][table_index])
        assert after[0][6] == before[0][6], "prior committed effects retain their exact rows"
        assert after[1:] == before[1:], "settlement invents no physical attempt or token usage"
        assert len(sdk_calls) == int(paid)
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_fresh_runtime_evidence_amplification_has_chunk_reason_when_complete_shelf_fits(operation_store, tmp_path, monkeypatch):
    from copy import copy

    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES

    arguments, runtime, sdk_calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        owner = runtime.live.turn_evidence.transcript
        base = owner.snapshot_for_continuation(runtime.live.llm_messages, materialization=runtime.materialization)
        evidence = runtime.source.evidence
        overhead = len(canonical_json(evidence.model_dump(mode="json")).encode()) - len(
            canonical_json(base.model_dump(mode="json")).encode()
        )
        assert overhead > 0
        target = JSON_MAX_TOTAL_UTF8_BYTES - overhead // 2
        amendments = [{"role": "user", "content": f"Amendment {number}: "} for number in range(3)]

        def prospective_shelf():
            # The sizing oracle runs the actual transcript producer on a
            # process-local prospective ledger; it grants no SQL authority.
            prospective = copy(owner)
            prospective._observed = list(owner._observed)
            prospective.observe_messages(amendments)
            return prospective.snapshot_for_continuation((*runtime.live.llm_messages, *amendments), materialization=runtime.materialization)

        fixed = len(canonical_json(prospective_shelf().model_dump(mode="json")).encode())
        remaining = (target - fixed) // 2
        assert 0 < remaining < 3 * 200_000
        for number, message in enumerate(amendments):
            amount = remaining // (len(amendments) - number)
            message["content"] += "x" * amount
            remaining -= amount
        expected_shelf = prospective_shelf()
        assert len(canonical_json(expected_shelf.model_dump(mode="json")).encode()) < JSON_MAX_TOTAL_UTF8_BYTES
        expected_evidence = evidence.model_copy(update={"transcript": expected_shelf})
        assert len(canonical_json(expected_evidence.model_dump(mode="json")).encode()) > JSON_MAX_TOTAL_UTF8_BYTES
        runtime.live.llm_messages.extend(amendments)
        owner.observe_messages(amendments)
        actual_shelf = owner.snapshot_for_continuation(runtime.live.llm_messages, materialization=runtime.materialization)
        assert actual_shelf == expected_shelf, "calibration uses the actual ordered event producer"
        before = full_rows(operation_store[0])
        with pytest.raises(ComposerContextUnsupported) as refused:
            await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        assert refused.value.reason == "retained_chunk_bytes"
        assert full_rows(operation_store[0]) == before and sdk_calls == []
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_final_source_integrity_outranks_reached_retention_refusal_and_keeps_both_originals(operation_store, tmp_path, monkeypatch):
    arguments, runtime, sdk_calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    amendments = [{"role": "user", "content": 'é\\"\n' * 24_000} for _ in range(3)]
    runtime.live.llm_messages.extend(amendments)
    runtime.live.turn_evidence.transcript.observe_messages(amendments)
    authority = arguments["authority"]
    actual_read = authority.read_continuation_runtime
    reads = []
    marker = AuditIntegrityError("required final source verification failed")

    def read(*args, **kwargs):
        reads.append(True)
        if len(reads) == 2:
            raise marker
        return actual_read(*args, **kwargs)

    monkeypatch.setattr(authority, "read_continuation_runtime", read)
    before = full_rows(operation_store[0])
    try:
        with pytest.raises(BaseExceptionGroup) as raised:
            await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        assert len(reads) == 2
        assert raised.value.exceptions[1] is marker
        refused = raised.value.exceptions[0]
        assert type(refused) is ComposerContextUnsupported and refused.reason == "retained_history_bytes"
        projected = project_composer_operation_error(raised.value, request_id=None)
        assert projected.http_status == 500 and projected.error_type == "audit_integrity_error"
        assert full_rows(operation_store[0]) == before and sdk_calls == []
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_changed_registered_source_outranks_large_fresh_shelf_even_at_equal_text_length(operation_store, tmp_path, monkeypatch):
    arguments, runtime, sdk_calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        registered = runtime.materialization._produced[0][0]
        assert type(registered) is dict and type(registered["content"]) is str and registered["content"]
        original = registered["content"]
        registered["content"] = ("b" if original[0] == "a" else "a") + original[1:]
        assert len(registered["content"]) == len(original)
        amendments = [{"role": "user", "content": 'é\\"\n' * 24_000} for _ in range(3)]
        runtime.live.llm_messages.extend(amendments)
        runtime.live.turn_evidence.transcript.observe_messages(amendments)
        before = full_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="registered message changed"):
            await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        assert full_rows(operation_store[0]) == before and sdk_calls == []
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("inject_excess", (False, True), ids=("supported-initial-owner", "injected-retention-excess"))
async def test_actual_initial_owner_retention_join_preserves_ready_cut_and_paid_title(
    operation_store, tmp_path, monkeypatch, inject_excess
):
    """Fault-inject only the fresh size observation; no natural-size claim."""
    from elspeth.contracts import bounded_serialization
    from elspeth.contracts.bounded_serialization import BoundedCanonicalSize
    from tests.unit.web.sessions.test_continuation_composition_initialization import construct, ready_case

    running, service, work, ready, sdk_calls = await ready_case(operation_store, tmp_path, monkeypatch, paid=True)
    actual_measure = bounded_serialization.bounded_canonical_json_size
    reached = []

    def measure(value, *, limit):
        measured = actual_measure(value, limit=limit)
        if type(value) is dict and "schema_version" in value and value["schema_version"] == "composer.transcript.v1":
            assert not measured.exceeded, "matched real initial input is supported before injection"
            reached.append(value)
            if inject_excess:
                return BoundedCanonicalSize(admitted_bytes=None, limit=limit)
        return measured

    monkeypatch.setattr(bounded_serialization, "bounded_canonical_json_size", measure)
    before = full_rows(operation_store[0])
    try:
        if inject_excess:
            with pytest.raises(ComposerContextUnsupported) as refused:
                await construct(operation_store, running, service, work, ready)
            assert refused.value.reason == "retained_history_bytes"
            assert full_rows(operation_store[0]) == before
            assert operation_store[2].load_continuation_snapshot(running) == ready
        else:
            runtime = await construct(operation_store, running, service, work, ready)
            assert runtime.source.selected.checkpoint_id != ready.checkpoint_id
        assert reached, "the actual first-Compose owner enters the fresh shelf preflight"
        assert len(sdk_calls) == 1, "the existing paid title is neither lost nor repeated"
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
