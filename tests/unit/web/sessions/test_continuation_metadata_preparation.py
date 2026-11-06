"""Detached metadata DATA from the current policy's actual paid PRIMARY."""

import asyncio
import time
from dataclasses import replace

import pytest
import rfc8785

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_metadata_preparation as preparation
from elspeth.web.composer import continuation_runtime as runtime_module
from elspeth.web.composer import tool_batch
from elspeth.web.composer.continuation_batch_data import decode_memory_completion_batch_data, encode_coupled_batch_data
from elspeth.web.composer.continuation_codec import encode_mandatory_private_chunk, encode_private_chunk
from elspeth.web.composer.continuation_iteration import decode_continuation_iteration
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES, ContinuationDocumentTooLarge
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_metadata_preparation import (
    MetadataPreparationRefused,
    complete_prepared_metadata_tool,
    prepare_metadata_tool_call,
)
from elspeth.web.composer.continuation_runtime import (
    build_runtime_tool_batch_context,
    capture_continuation_runtime_owners,
    capture_metadata_preparation_runtime,
    restore_metadata_preparation_runtime,
)
from elspeth.web.composer.continuation_tool_receipts import invocation_from_receipt
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.required_work import RequiredWorkSource, required_failure_leaves
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case

operation_store = test_composer_operation_authority.operation_store


async def _detached_inputs(owner, selected):
    original, inventory = await owner.current(selected)
    assert original.graph.batch is not None
    cold_provider = ProviderInvocationOwner(service=owner.service._require_sessions_service(), required_work=owner.work)
    cold = ContinuationLivePhases(owner.service, owner.authority, owner.work, cold_provider, owner.lease)
    detached = await restore_metadata_preparation_runtime(
        service=owner.service,
        authority=owner.authority,
        selected=selected,
        inventory=inventory,
        required_work=owner.work,
        provider_owner=cold_provider,
    )
    cold.runtime = detached
    assert detached.graph.batch is not None
    primary = await cold._primary_for_phase(detached, inventory, selected)
    arguments = {
        "runtime": detached,
        "original_runtime": original,
        "call_model": primary.outcome,
        "cancellation_requested": cold.cancellation_requested,
    }
    return original, detached, arguments


@pytest.mark.asyncio
async def test_metadata_preparation_finishes_once_without_changing_original_or_sql(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, _running, _sdk):
        selected = owner.runtime.source.selected
        original, detached, arguments = await _detached_inputs(owner, selected)
        data = original.graph.batch
        assert data is not None
        source_batch = encode_coupled_batch_data(batch=data.batch, accumulator=data.accumulator, calls=data.calls)
        source_recorder = original.live.recorder.snapshot_for_continuation(drained=original.drained)
        source_anchor = original.live.anti_anchor.snapshot_for_continuation()
        source_state = original.live.state
        before = recovery_rows(store[0])
        computed = []
        actual = preparation.prepare_set_metadata_result_value

        def compute(*args, **kwargs):
            result = actual(*args, **kwargs)
            computed.append(result)
            return result

        def forbidden(*args, **kwargs):
            raise AssertionError("detached metadata preparation entered generic execution")

        monkeypatch.setattr(preparation, "prepare_set_metadata_result_value", compute)
        monkeypatch.setattr(tool_batch, "execute_tool", forbidden)
        prepared = await prepare_metadata_tool_call(**arguments)
        assert computed == []
        assert detached.live.recorder.invocations == original.live.recorder.invocations
        original_json = prepared.prepared_batch_json
        observed_before = time.monotonic_ns()
        await asyncio.sleep(0.002)
        observed_after = time.monotonic_ns()
        assert observed_after > observed_before
        minimum_latency_ms = (observed_after - prepared.original_audit.started_ns) // 1_000_000
        completed = await complete_prepared_metadata_tool(prepared)
        assert len(computed) == 1
        assert completed.result is computed[0]
        assert prepared.call.audit is not None
        invocation = invocation_from_receipt(completed.receipt, audit=prepared.call.audit, env_ref_names=prepared.runtime.env_ref_names)
        memory = decode_memory_completion_batch_data(completed.memory_completion_json, env_ref_names=prepared.runtime.env_ref_names)
        assert memory.batch.current_call is not None
        assert memory.batch.current_call.phase == "application_pending"
        assert invocation.tool_call_id == prepared.call.tool_call.id
        assert invocation.started_at == prepared.call.audit.started_at
        assert invocation.finished_at >= invocation.started_at
        assert invocation.latency_ms >= minimum_latency_ms
        assert prepared.prepared_batch_json == original_json
        assert prepared.call.audit is prepared.original_audit
        assert prepared.original_audit == prepared.original_audit_fields
        completed_data = prepared.runtime.graph.batch
        assert completed_data is not None
        assert completed_data.batch.next_tool_index == 1 and completed_data.batch.phase == "dispatch_ready"
        assert completed_data.batch.current_call is None
        assert completed.result.updated_state.version == source_state.version + 1
        assert completed_data.batch.tool_outcomes[-1].response is completed.result
        assert prepared.runtime.live.llm_messages[-1]["tool_call_id"] == prepared.call.tool_call.id
        with pytest.raises(AuditIntegrityError, match="original detached prepared graph"):
            await complete_prepared_metadata_tool(prepared)
        assert len(computed) == 1
        assert original.live.state is source_state
        assert encode_coupled_batch_data(batch=data.batch, accumulator=data.accumulator, calls=data.calls) == source_batch
        assert original.live.recorder.snapshot_for_continuation(drained=original.drained) == source_recorder
        assert original.live.anti_anchor.snapshot_for_continuation() == source_anchor
        assert recovery_rows(store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ("batch", "recorder", "messages", "anti_anchor"))
async def test_metadata_preparation_rejects_original_mutable_owner(operation_store, tmp_path, monkeypatch, alias):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, _running, _sdk):
        selected = owner.runtime.source.selected
        original, detached, arguments = await _detached_inputs(owner, selected)
        before = recovery_rows(store[0])
        if alias == "batch":
            arguments["runtime"] = replace(detached, graph=detached.graph.with_batch(original.graph.batch))
        elif alias == "recorder":
            arguments["runtime"] = replace(detached, live=replace(detached.live, recorder=original.live.recorder))
        elif alias == "messages":
            arguments["runtime"] = replace(detached, live=replace(detached.live, llm_messages=original.live.llm_messages))
        else:
            arguments["runtime"] = replace(detached, live=replace(detached.live, anti_anchor=original.live.anti_anchor))
        with pytest.raises(AuditIntegrityError, match="original mutable owner"):
            await prepare_metadata_tool_call(**arguments)
        assert recovery_rows(store[0]) == before


@pytest.mark.asyncio
async def test_metadata_preparation_refuses_explicit_approval_before_prepare(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (_store, owner, _running, _sdk):
        selected = owner.runtime.source.selected
        _original, detached, arguments = await _detached_inputs(owner, selected)
        build_context = preparation.build_runtime_tool_batch_context

        async def explicit_context(*args, **kwargs):
            return replace(await build_context(*args, **kwargs), review_required=True)

        monkeypatch.setattr(preparation, "build_runtime_tool_batch_context", explicit_context)
        before = detached.live.recorder.snapshot_for_continuation(drained=detached.drained)
        with pytest.raises(MetadataPreparationRefused, match="automatic unissued call"):
            await prepare_metadata_tool_call(**arguments)
        assert detached.live.recorder.snapshot_for_continuation(drained=detached.drained) == before
        assert detached.graph.batch.batch.phase == "call_ready"


@pytest.mark.asyncio
async def test_metadata_completion_rejects_changed_prepared_graph_without_computation(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (_store, owner, _running, _sdk):
        selected = owner.runtime.source.selected
        _original, _detached, arguments = await _detached_inputs(owner, selected)
        prepared = await prepare_metadata_tool_call(**arguments)
        prepared.call.arguments["patch"] = {"name": "different than retained preparation"}

        def forbidden(*args, **kwargs):
            raise AssertionError("changed prepared source reached computation")

        monkeypatch.setattr(preparation, "prepare_set_metadata_result_value", forbidden)
        with pytest.raises(AuditIntegrityError):
            await complete_prepared_metadata_tool(prepared)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("identity", "started_ns", "actor", "portable_elapsed"))
async def test_metadata_completion_rejects_changed_original_audit_before_computation(operation_store, tmp_path, monkeypatch, change):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, _running, _sdk):
        selected = owner.runtime.source.selected
        _original, detached, arguments = await _detached_inputs(owner, selected)
        prepared = await prepare_metadata_tool_call(**arguments)
        before = recovery_rows(store[0])
        recorder_before = detached.live.recorder.snapshot_for_continuation(drained=detached.drained)
        if change == "identity":
            prepared.call.audit = replace(prepared.original_audit)
        elif change == "portable_elapsed":
            prepared = replace(
                prepared,
                portable_audit=prepared.portable_audit.model_copy(
                    update={"elapsed_active_ns": prepared.portable_audit.elapsed_active_ns + 1}
                ),
            )
        else:
            changed_audit = (
                replace(prepared.original_audit, started_ns=prepared.original_audit.started_ns + 1)
                if change == "started_ns"
                else replace(prepared.original_audit, actor="changed-metadata-actor")
            )
            prepared.call.audit = changed_audit
            # Keep the identity arm satisfied deliberately: the independent
            # immutable field snapshot must catch changed timer/binding DATA.
            prepared = replace(prepared, original_audit=changed_audit)

        def forbidden(*args, **kwargs):
            raise AssertionError("changed original metadata audit reached computation")

        monkeypatch.setattr(preparation, "prepare_set_metadata_result_value", forbidden)
        with pytest.raises(AuditIntegrityError, match="original detached prepared graph"):
            await complete_prepared_metadata_tool(prepared)
        assert detached.live.recorder.snapshot_for_continuation(drained=detached.drained) == recorder_before
        assert recovery_rows(store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_read", (None, 1, 2))
async def test_metadata_restore_keeps_shared_disclosure_and_original_source_reads(operation_store, tmp_path, monkeypatch, reject_read):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, driver, running, _sdk):
        selected = driver.runtime.source.selected
        original, inventory = await driver.current(selected)
        sid = str(running.claim.session_id)
        shared = driver.service._schema_disclosure
        # Distinctive existing in-memory guidance is intentionally absent from
        # the stored source. A normal restore would replace this marker.
        shared.mark_plugin_schema_loaded(sid, "transform", "shared-before-metadata-preparation")
        shared.mark_plugin_schema_loaded("another-session", "source", "shared-other-session")
        before = shared.schemas_loaded_for_session(sid)
        other_before = shared.schemas_loaded_for_session("another-session")
        sql_before = recovery_rows(store[0])
        reads = []
        read_source = type(driver.authority).read_continuation_runtime
        read_restoration = type(driver.authority).read_continuation_runtime_restoration

        def observe_source(self, actual_running, **kwargs):
            result = read_source(self, actual_running, **kwargs)
            reads.append((actual_running, kwargs["selected"], kwargs["inventory"]))
            if reject_read == len(reads):
                raise AuditIntegrityError("controlled original metadata source read refusal")
            return result

        def observe_restoration(self, actual_running, **kwargs):
            result = read_restoration(self, actual_running, **kwargs)
            reads.append((actual_running, kwargs["selected"], kwargs["inventory"]))
            if reject_read == len(reads):
                raise AuditIntegrityError("controlled original metadata source read refusal")
            return result

        monkeypatch.setattr(type(driver.authority), "read_continuation_runtime", observe_source)
        monkeypatch.setattr(type(driver.authority), "read_continuation_runtime_restoration", observe_restoration)
        provider = ProviderInvocationOwner(service=store[3], required_work=driver.work)
        if reject_read is not None:
            with pytest.raises(AuditIntegrityError, match="controlled original metadata source read refusal"):
                await restore_metadata_preparation_runtime(
                    service=driver.service,
                    authority=driver.authority,
                    selected=selected,
                    inventory=inventory,
                    required_work=driver.work,
                    provider_owner=provider,
                )
            assert len(reads) == reject_read
        else:
            detached = await restore_metadata_preparation_runtime(
                service=driver.service,
                authority=driver.authority,
                selected=selected,
                inventory=inventory,
                required_work=driver.work,
                provider_owner=provider,
            )
            assert len(reads) == 2
            assert detached.source.selected is selected
            assert detached.detached_disclosure is not None and detached.detached_disclosure is not shared
            assert detached.detached_disclosure.schemas_loaded_for_session(sid) == frozenset(original.source.iteration.disclosure.schemas)
            ctx = await build_runtime_tool_batch_context(detached, cancellation_requested=driver.cancellation_requested)
            assert ctx.schema_disclosure is detached.detached_disclosure
            detached.detached_disclosure.mark_plugin_schema_loaded(sid, "transform", "detached-only-metadata-preparation")
            captured = await capture_continuation_runtime_owners(detached, selected=selected, inventory=inventory)
            iteration_ref = captured.stack.frames[0].iteration
            iteration = decode_continuation_iteration(
                next(chunk for chunk in captured.chunks if chunk.reference == iteration_ref), env_ref_names=inventory.names
            )
            assert ("transform", "detached-only-metadata-preparation") in iteration.disclosure.schemas
            assert ("transform", "shared-before-metadata-preparation") not in iteration.disclosure.schemas
        assert all(
            actual_running is running and actual_selected is selected and actual_inventory is inventory
            for actual_running, actual_selected, actual_inventory in reads
        )
        assert shared.schemas_loaded_for_session(sid) == before
        assert shared.schemas_loaded_for_session("another-session") == other_before
        assert recovery_rows(store[0]) == sql_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "reject_read"),
    (
        ("fit", None),
        ("mandatory_fit", None),
        ("document_over", None),
        ("mandatory_over", None),
        ("malformed", None),
        ("integrity", None),
        ("fit", 1),
        ("fit", 2),
        ("document_over", 2),
    ),
)
async def test_metadata_capture_keeps_size_refusal_clean_and_source_failures_honest(
    operation_store, tmp_path, monkeypatch, boundary, reject_read
):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, driver, running, _sdk):
        selected = driver.runtime.source.selected
        _original, detached, _arguments = await _detached_inputs(driver, selected)
        inventory = detached.materialization.inventory
        sid = str(running.claim.session_id)
        shared = driver.service._schema_disclosure
        shared.mark_plugin_schema_loaded(sid, "transform", "shared-capture-marker")
        shared.mark_plugin_schema_loaded("another-session", "source", "shared-other-capture-marker")
        shared_before = shared.schemas_loaded_for_session(sid)
        other_before = shared.schemas_loaded_for_session("another-session")
        assert detached.detached_disclosure is not None
        detached.detached_disclosure.mark_plugin_schema_loaded(sid, "transform", "private-capture-marker")
        private_before = detached.detached_disclosure.schemas_loaded_for_session(sid)
        sql_before = recovery_rows(store[0])
        coordinator = driver.work.coordinator
        tickets_before = coordinator.tickets
        reads = []
        codec_results = []
        source_failure = AuditIntegrityError("controlled metadata capture source refusal")
        read_source = type(driver.authority).read_continuation_runtime
        capture_owners = runtime_module._capture_owners

        # Exercise the unchanged real private-document encoders at the capture
        # seam. This boundary document is test DATA, never a selected chunk or
        # a fabricated runtime source, and does not replace the actual capture.
        assert CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES == 1_048_576
        payload = {"padding": ""}
        overhead = len(canonical_json(payload).encode("utf-8"))
        over = int(boundary in ("document_over", "mandatory_over"))
        payload["padding"] = "x" * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES - overhead + over)
        canonical = canonical_json(payload)
        assert len(canonical.encode("utf-8")) == CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + over

        def observe_source(self, actual_running, **kwargs):
            result = read_source(self, actual_running, **kwargs)
            reads.append((actual_running, kwargs["selected"], kwargs["inventory"]))
            if len(reads) == reject_read:
                raise source_failure
            return result

        def capture_with_codec(actual_runtime, **kwargs):
            captured = capture_owners(actual_runtime, **kwargs)
            if boundary in ("mandatory_fit", "mandatory_over"):
                chunk = encode_mandatory_private_chunk(codec="composer.messages.v1", value=payload)
            else:
                chunk = encode_private_chunk(codec="composer.messages.v1", canonical="{" if boundary == "malformed" else canonical)
            codec_results.append(chunk)
            return captured

        monkeypatch.setattr(type(driver.authority), "read_continuation_runtime", observe_source)
        monkeypatch.setattr(runtime_module, "_capture_owners", capture_with_codec)
        if boundary == "integrity":
            detached = replace(detached, live=replace(detached.live, initial_version=detached.live.initial_version + 1))
        failed = boundary in ("malformed", "integrity") or reject_read is not None
        if failed:
            expected = ExceptionGroup if boundary == "document_over" and reject_read == 2 else AuditIntegrityError
            with pytest.raises(expected) as caught:
                await capture_metadata_preparation_runtime(detached, selected=selected, inventory=inventory)
            leaves = required_failure_leaves(caught.value)
            if reject_read is not None:
                assert source_failure in leaves
            else:
                assert all(type(leaf) is AuditIntegrityError for leaf in leaves)
            if boundary == "document_over":
                assert any(type(leaf) is ContinuationDocumentTooLarge for leaf in leaves)
        else:
            captured = await capture_metadata_preparation_runtime(detached, selected=selected, inventory=inventory)
            if boundary in ("fit", "mandatory_fit"):
                assert captured is not None
                assert len(codec_results) == 1
                assert codec_results[0].reference.byte_length == CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
                assert codec_results[0].canonical == canonical
                iteration_ref = captured.stack.frames[0].iteration
                iteration = decode_continuation_iteration(
                    next(chunk for chunk in captured.chunks if chunk.reference == iteration_ref), env_ref_names=inventory.names
                )
                assert ("transform", "private-capture-marker") in iteration.disclosure.schemas
                assert ("transform", "shared-capture-marker") not in iteration.disclosure.schemas
            else:
                assert captured is None and codec_results == []
        assert len(reads) == (1 if reject_read == 1 or boundary in ("malformed", "integrity") else 2)
        assert all(
            actual_running is running and actual_selected is selected and actual_inventory is inventory
            for actual_running, actual_selected, actual_inventory in reads
        )
        tickets = tuple(
            ticket
            for ticket in coordinator.tickets
            if ticket not in tickets_before
            and ticket.key.source in (RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
        )
        assert len(tickets) == 2 and all(ticket.complete for ticket in tickets)
        if failed:
            assert any(ticket.errors for ticket in tickets)
        else:
            assert all(ticket.errors == () and ticket.receipts() == () for ticket in tickets)
        assert shared.schemas_loaded_for_session(sid) == shared_before
        assert shared.schemas_loaded_for_session("another-session") == other_before
        assert detached.detached_disclosure.schemas_loaded_for_session(sid) == private_before
        assert recovery_rows(store[0]) == sql_before


def test_metadata_generated_private_codec_rejects_malformed_tail_before_size_refusal():
    # A malformed value after a prefix exceeding 1 MiB must remain an error;
    # it cannot acquire the clean size-only refusal classification.
    malformed = ["x" * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1), object()]
    with pytest.raises(rfc8785.CanonicalizationError) as caught:
        encode_mandatory_private_chunk(codec="composer.messages.v1", value=malformed)
    assert not isinstance(caught.value, ComposerContextUnsupported)
