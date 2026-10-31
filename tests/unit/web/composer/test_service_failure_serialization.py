"""A live service original never becomes continuation DATA or a restored error."""

import json

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_application_graph import (
    ApplicationGraph,
    capture_application_recorder_source,
    decode_application_graph,
    encode_application_graph,
)
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    decode_coupled_batch_chunk,
    decode_coupled_batch_data,
    decode_memory_completion_batch_data,
    decode_prepared_batch_data,
    encode_coupled_batch_chunk,
    encode_coupled_batch_data,
    encode_memory_completion_batch_data,
    encode_prepared_batch_data,
    encode_restored_memory_completion_batch_data,
    encode_restored_prepared_batch_data,
    readmit_prepared_batch_audit,
)
from elspeth.web.composer.continuation_p4_batch import (
    decode_p4_pending_batch_chunk,
    decode_p4_pending_batch_data,
    encode_p4_pending_batch_chunk,
    encode_p4_pending_batch_data,
)
from tests.unit.web.composer.test_continuation_active_evidence_graph import _actual_portable_p4_input
from tests.unit.web.composer.test_continuation_batch_data import actual_cut
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence
from tests.unit.web.composer.test_continuation_memory_completion_batch_data import completed_cut
from tests.unit.web.composer.test_continuation_prepared_batch_data import prepared_cut
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


def _original():
    original = OperationalError("private service statement", {}, RuntimeError("private driver original"))
    cause = RuntimeError("preexisting service cause")
    original.__cause__ = cause
    return original, cause


@pytest.mark.parametrize("route", ("coupled_data", "coupled_chunk", "application_graph"))
def test_live_service_original_refuses_each_otherwise_valid_quiescent_route(route):
    batch, acc, calls = actual_cut()
    evidence, _messages, recorder, _logical, _policy = rich_evidence()
    graph = ApplicationGraph(
        RestoredBatchData(batch, acc, calls), evidence.effects, recorder.snapshot_for_continuation(drained=RecorderDrainCursors()), ()
    )
    recorder_source = capture_application_recorder_source(graph.recorder, env_ref_names=frozenset())

    def encode():
        if route == "coupled_data":
            return encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)
        if route == "coupled_chunk":
            return encode_coupled_batch_chunk(batch=batch, accumulator=acc, calls=calls)
        assert route == "application_graph"
        return encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=recorder_source)

    baseline = encode()
    if route == "coupled_data":
        restored = decode_coupled_batch_data(baseline)
    elif route == "coupled_chunk":
        restored = decode_coupled_batch_chunk(baseline)
    else:
        restored = decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=recorder_source).selected
    assert restored.batch.service_failure is None
    assert restored.batch.next_tool_index == batch.next_tool_index
    assert restored.batch.tool_outcomes[0].response is restored.batch.tool_outcomes[1].response

    original, cause = _original()
    batch.service_failure = original
    with pytest.raises(AuditIntegrityError, match="live service failure"):
        encode()
    assert batch.service_failure is original and original.__cause__ is cause
    # Restore the test's one changed input; supported DATA must stay byte-identical.
    batch.service_failure = None
    assert encode() == baseline


@pytest.mark.parametrize("route", ("prepared", "memory_completion"))
def test_live_service_original_refuses_actual_prepared_and_completed_memory_routes(monkeypatch, route):
    if route == "prepared":
        # This supplier's finite clock describes one capture followed by a
        # separate readmission. Keep its setup local; do not extend its ticks.
        with monkeypatch.context() as setup:
            batch, acc, calls, dialect = prepared_cut(setup)
            baseline = encode_prepared_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)
        historical = decode_prepared_batch_data(baseline)
    else:
        batch, acc, calls, dialect, _invocation = completed_cut(monkeypatch)
        baseline = encode_memory_completion_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)
        historical = decode_memory_completion_batch_data(baseline)

    def encode_historical():
        if route == "prepared":
            return encode_restored_prepared_batch_data(historical)
        return encode_restored_memory_completion_batch_data(historical)

    assert historical.batch.service_failure is None
    assert historical.batch.current_call is not None and historical.batch.current_call.audit is None
    assert encode_historical() == baseline
    original, cause = _original()
    historical.batch.service_failure = original
    with pytest.raises(AuditIntegrityError, match="live service failure"):
        encode_historical()
    assert historical.batch.service_failure is original and original.__cause__ is cause
    # Restore only the one deliberately changed test input. Exact historical
    # bytes remain the oracle on this same owner, without recapturing a timer.
    historical.batch.service_failure = None
    assert encode_historical() == baseline

    if route == "prepared":
        # Independent genuine DATA supplies the live trial. The ordinary
        # readmission API owns its current anchor and historical elapsed work;
        # no failed live batch is cloned/cleared into historical serialization.
        live = decode_prepared_batch_data(baseline)
        call = live.batch.current_call
        assert call is not None and call.audit is None
        audit = readmit_prepared_batch_audit(
            live,
            selected_call=call.tool_call,
            selected_pre_state=call.pre_state,
            current_dialect=live.dialect,
            actor=live.audit.actor,
            strict_sent=call.wire.strict_sent,
            wire_conformant=call.wire.wire_conformant,
        )
        call.audit = audit
        batch, acc, calls, dialect = live.batch, live.accumulator, live.calls, live.dialect
    call = batch.current_call
    assert call is not None and call.audit is not None
    actual_audit = call.audit
    original_anchor = actual_audit.started_ns

    def encode_live():
        if route == "prepared":
            return encode_prepared_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)
        return encode_memory_completion_batch_data(batch=batch, accumulator=acc, calls=calls, dialect=dialect)

    def require_complete_live_roundtrip(encoded):
        if route == "prepared":
            restored = decode_prepared_batch_data(encoded)
            assert encode_restored_prepared_batch_data(restored) == encoded
            assert restored.audit.started_at == historical.audit.started_at
            assert restored.audit.elapsed_active_ns >= historical.audit.elapsed_active_ns
        else:
            restored = decode_memory_completion_batch_data(encoded)
            assert encode_restored_memory_completion_batch_data(restored) == encoded
        assert restored.batch.service_failure is None
        assert restored.batch.next_tool_index == batch.next_tool_index
        assert restored.batch.current_call.tool_call == call.tool_call
        assert call.audit is actual_audit and actual_audit.started_ns == original_anchor

    live_baseline = encode_live()
    require_complete_live_roundtrip(live_baseline)
    if route == "memory_completion":
        assert live_baseline == baseline
    batch.service_failure = original
    with pytest.raises(AuditIntegrityError, match="live service failure"):
        encode_live()
    assert batch.service_failure is original and original.__cause__ is cause
    assert call.audit is actual_audit and actual_audit.started_ns == original_anchor
    batch.service_failure = None
    after = encode_live()
    require_complete_live_roundtrip(after)
    if route == "memory_completion":
        assert after == baseline


@pytest.mark.asyncio
async def test_live_service_original_refuses_actual_writer_pending_and_application_routes(operation_store, tmp_path, monkeypatch):
    # The ordinary writer really produced/committed this pending input and its
    # application graph. A fabricated completed batch could miss the real codec.
    async with _actual_portable_p4_input(operation_store, tmp_path, monkeypatch) as (values, _bundle):
        names = values["env_ref_names"]
        data = decode_p4_pending_batch_chunk(values["pending_batch_chunk"], env_ref_names=names)
        for route in ("p4_data", "p4_chunk", "pending_application_graph", "historical_application_graph"):
            graph = _bundle.evidence_after.graph if route == "historical_application_graph" else values["evidence_snapshot"].graph
            assert graph is not None
            recorder_source = capture_application_recorder_source(graph.recorder, env_ref_names=names)
            if route == "historical_application_graph":
                assert graph.cuts
                failed = graph.cuts[0].data.batch
            else:
                assert graph.selected.batch.phase == "p4_pending"
                failed = graph.selected.batch if route == "pending_application_graph" else data.batch

            def encode(data=data, graph=graph, names=names, route=route, recorder_source=recorder_source):
                if route == "p4_data":
                    return encode_p4_pending_batch_data(
                        batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=names
                    )
                if route == "p4_chunk":
                    return encode_p4_pending_batch_chunk(
                        batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=names
                    )
                return encode_application_graph(graph, env_ref_names=names, recorder_source=recorder_source)

            baseline = encode()
            if route == "p4_data":
                restored = decode_p4_pending_batch_data(baseline, env_ref_names=names)
            elif route == "p4_chunk":
                restored = decode_p4_pending_batch_chunk(baseline, env_ref_names=names)
            else:
                restored = decode_application_graph(baseline, env_ref_names=names, recorder_source=recorder_source).selected
            assert restored.batch.service_failure is None
            original, cause = _original()
            failed.service_failure = original
            with pytest.raises(AuditIntegrityError, match="live service failure"):
                encode()
            assert failed.service_failure is original and original.__cause__ is cause
            failed.service_failure = None
            assert encode() == baseline


def test_live_service_failure_is_not_a_retained_failure_descriptor():
    batch, acc, calls = actual_cut()
    baseline = encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)
    restored = decode_coupled_batch_data(baseline)
    assert restored.batch.retained_failure.kind == "advisor_timeout" and restored.batch.service_failure is None
    payload = json.loads(baseline)
    descriptors = [node for node in payload["nodes"] if node["kind"] == "RetainedBatchFailure"]
    assert len(descriptors) == 1
    members = dict(descriptors[0]["value"])
    assert members["kind"] == {"scalar": "advisor_timeout"}
    members["kind"]["scalar"] = "service_failure"
    with pytest.raises(AuditIntegrityError, match="failure"):
        decode_coupled_batch_data(canonical_json(payload))
    original, cause = _original()
    batch.service_failure = original
    with pytest.raises(AuditIntegrityError, match="live service failure"):
        encode_coupled_batch_data(batch=batch, accumulator=acc, calls=calls)
    assert batch.service_failure is original and original.__cause__ is cause
