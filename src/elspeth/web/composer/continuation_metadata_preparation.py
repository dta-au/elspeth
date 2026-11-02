"""Detached DATA preparation for one provider-authored metadata mutation.

These values are not issue or publication authority. The owning session path
must admit the entire retained tail and prove the original source before it
can adopt any of the detached mutable owners.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import JsonValue

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.audit import DispatchAudit, dispatch_with_audit
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    RestoredPreparedBatchData,
    decode_prepared_batch_data,
    encode_memory_completion_batch_data,
    encode_prepared_batch_data,
    encode_restored_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV3, ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, build_runtime_tool_batch_context
from elspeth.web.composer.continuation_tool_receipts import RetainedToolReceipt, capture_tool_receipt, encode_retained_tool_receipt
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tool_batch import (
    ContinuationCallState,
    ToolBatchContext,
    _append_continuation_tool_outcome,
    _finalize_completed_incremental_mutation,
    _prepare_continuation_call_live,
    completed_tool_state,
    record_completed_tool_anchor,
)
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools._dispatch import prepare_set_metadata_result_value
from elspeth.web.interpretation_state import interpretation_sites


class MetadataPreparationRefused(ValueError):
    """The original tool requires a capability outside this exact pure slice."""


@dataclass(frozen=True, slots=True)
class PreparedMemoryToolCall:
    call_model: _CallModelOutcome
    context: ToolBatchContext
    runtime: RestoredContinuationRuntime
    call: ContinuationCallState
    prepared_batch_json: str
    original_audit: DispatchAudit
    original_audit_fields: DispatchAudit
    portable_audit: PortableDispatchAudit


@dataclass(frozen=True, slots=True)
class PreparedMetadataToolCall(PreparedMemoryToolCall):
    """The original single metadata preparation, without SQL authority."""


@dataclass(frozen=True, slots=True)
class PreparedMemoryToolCompletion:
    prepared: PreparedMemoryToolCall
    result: ToolResult
    receipt: RetainedToolReceipt
    memory_completion_json: str


@dataclass(frozen=True, slots=True)
class PreparedMetadataToolCompletion(PreparedMemoryToolCompletion):
    prepared: PreparedMetadataToolCall


async def prepare_metadata_tool_call(
    *,
    runtime: RestoredContinuationRuntime,
    original_runtime: RestoredContinuationRuntime,
    call_model: _CallModelOutcome,
    cancellation_requested: asyncio.Event,
) -> PreparedMetadataToolCall:
    prepared = await prepare_memory_tool_call(
        runtime=runtime,
        original_runtime=original_runtime,
        call_model=call_model,
        cancellation_requested=cancellation_requested,
        tool_name="set_metadata",
        prepared_type=PreparedMetadataToolCall,
    )
    if type(prepared) is not PreparedMetadataToolCall:
        raise AuditIntegrityError("metadata preparation replaced its nominal prepared value")
    return prepared


async def prepare_memory_tool_call(
    *,
    runtime: RestoredContinuationRuntime,
    original_runtime: RestoredContinuationRuntime,
    call_model: _CallModelOutcome,
    cancellation_requested: asyncio.Event,
    tool_name: Literal["set_metadata", "set_pipeline"],
    prepared_type: type[PreparedMemoryToolCall],
) -> PreparedMemoryToolCall:
    """Prepare on an independently restored graph before any SQL issue."""
    if type(runtime) is not RestoredContinuationRuntime or type(original_runtime) is not RestoredContinuationRuntime:
        raise TypeError("metadata preparation requires its actual runtime owners")
    data, original_data = runtime.graph.batch, original_runtime.graph.batch
    if type(data) is not RestoredBatchData or type(original_data) is not RestoredBatchData:
        raise AuditIntegrityError("metadata preparation lost its original coupled batch")
    if (
        runtime.source.selected != original_runtime.source.selected
        or runtime.env_ref_names != original_runtime.env_ref_names
        or runtime.bindings.session_operation_context is not original_runtime.bindings.session_operation_context
        or runtime.bindings.required_work is not original_runtime.bindings.required_work
        or data.calls != original_data.calls
    ):
        raise AuditIntegrityError("metadata preparation changed its actual original source")
    batch, acc = data.batch, data.accumulator
    original_batch = original_data.batch
    llm_messages = runtime.live.llm_messages
    original_message_ids = {id(message) for message in original_runtime.live.llm_messages}
    if (
        runtime is original_runtime
        or runtime.live is original_runtime.live
        or runtime.materialization is original_runtime.materialization
        or batch is original_batch
        or acc is original_data.accumulator
        or batch.tool_outcomes is original_batch.tool_outcomes
        or batch.private_authorings_by_call is original_batch.private_authorings_by_call
        or batch.decoded_args_by_call_id is original_batch.decoded_args_by_call_id
        or batch.refused_resources is original_batch.refused_resources
        or batch.promises_by_call is original_batch.promises_by_call
        or runtime.live.recorder is original_runtime.live.recorder
        or runtime.live.anti_anchor is original_runtime.live.anti_anchor
        or runtime.live.turn_evidence is original_runtime.live.turn_evidence
        or llm_messages is original_runtime.live.llm_messages
        or any(id(message) in original_message_ids for message in llm_messages)
    ):
        raise AuditIntegrityError("metadata preparation reused an original mutable owner")
    calls = call_model.completion.tool_batch.calls
    if (
        type(runtime.source.policy) not in (ContinuationAdmissionPolicyV3, ContinuationAdmissionPolicyV4)
        or len(calls) != 1
        or calls != data.calls
        or calls[0] is not data.calls[0]
        or calls[0].function.name != tool_name
        or (tool_name == "set_pipeline" and type(runtime.source.policy) is not ContinuationAdmissionPolicyV4)
        or batch.phase != "call_ready"
        or batch.next_tool_index != 0
        or batch.current_call is not None
        or batch.tool_outcomes
        or batch.stop_issued_batch
        or batch.refused_resources
        or acc.authority_revocation is not None
        or (batch.state.sources and batch.state.outputs and interpretation_sites(batch.state))
    ):
        raise MetadataPreparationRefused("metadata preparation requires one automatic unissued call without runtime preflight")
    ctx = await build_runtime_tool_batch_context(runtime, cancellation_requested=cancellation_requested)
    if ctx.review_required or ctx.mutation_ceiling is None:
        raise MetadataPreparationRefused("metadata preparation requires one automatic unissued call without runtime preflight")
    if ctx.message_producers is None:
        raise AuditIntegrityError("metadata preparation lost its actual original-source message producer")
    call = ContinuationCallState(0, calls[0], batch.state, batch.state.version)
    batch.current_call, batch.phase = call, "call_active"
    status = await _prepare_continuation_call_live(call_model=call_model, ctx=ctx, llm_messages=llm_messages, batch=batch, call=call)
    if status != "prepared":
        raise MetadataPreparationRefused("metadata arguments do not have a complete prepared dispatch")
    encoded = encode_prepared_batch_data(
        batch=batch, accumulator=acc, calls=calls, dialect=ctx.tool_contract_dialect, env_ref_names=runtime.env_ref_names
    )
    if type(call.audit) is not DispatchAudit:
        raise AuditIntegrityError("metadata preparation lost its original live audit")
    portable = decode_prepared_batch_data(encoded, env_ref_names=runtime.env_ref_names).audit
    return prepared_type(call_model, ctx, runtime, call, encoded, call.audit, replace(call.audit), portable)


async def complete_prepared_metadata_tool(prepared: PreparedMetadataToolCall) -> PreparedMetadataToolCompletion:
    """Compute and finish once, then fold only the bound detached graph."""
    if type(prepared) is not PreparedMetadataToolCall:
        raise TypeError("metadata completion requires its original prepared DATA")

    async def compute() -> ToolResult:
        data = prepared.runtime.graph.batch
        if type(data) is not RestoredBatchData:
            raise AuditIntegrityError("metadata completion lost its detached coupled batch")
        call, ctx, batch = prepared.call, prepared.context, data.batch
        candidate = await run_sync_in_worker(
            prepare_set_metadata_result_value,
            call.arguments,
            call.pre_state,
            ctx.policy_catalog,
            plugin_snapshot=ctx.plugin_snapshot,
            prior_validation=batch.last_validation,
        )
        candidate = await _finalize_completed_incremental_mutation(
            candidate, call.pre_state, plugin_snapshot=ctx.plugin_snapshot, policy_catalog=ctx.policy_catalog
        )
        if (
            not candidate.success
            or candidate.updated_state.version != call.pre_version + 1
            or (candidate.updated_state.sources and candidate.updated_state.outputs and interpretation_sites(candidate.updated_state))
        ):
            raise MetadataPreparationRefused("metadata result requires an unsupported result or runtime preflight tail")
        return candidate

    completed = await complete_prepared_memory_tool(prepared, compute=compute)
    return PreparedMetadataToolCompletion(prepared, completed.result, completed.receipt, completed.memory_completion_json)


async def complete_prepared_memory_tool(
    prepared: PreparedMemoryToolCall, *, compute: Callable[[], Awaitable[ToolResult]]
) -> PreparedMemoryToolCompletion:
    """Complete only a closed nominal value-tool preparation on detached owners."""
    from elspeth.web.composer.continuation_pipeline_preparation import PreparedPipelineToolCall

    if type(prepared) not in (PreparedMetadataToolCall, PreparedPipelineToolCall):
        raise TypeError("memory completion requires its original closed prepared DATA")
    data = prepared.runtime.graph.batch
    if type(data) is not RestoredBatchData:
        raise AuditIntegrityError("metadata completion lost its detached coupled batch")
    ctx, batch, call = prepared.context, data.batch, prepared.call
    calls = prepared.call_model.completion.tool_batch.calls
    names = prepared.runtime.env_ref_names
    if (
        batch.current_call is not call
        or type(call.audit) is not DispatchAudit
        or call.audit is not prepared.original_audit
        or call.audit != prepared.original_audit_fields
        or call.arguments is None
    ):
        raise AuditIntegrityError("metadata completion changed its original detached prepared graph")
    # Compare complete DATA with the original portable timing observation.
    # Copy only the two root carriers to keep the live audit untouched and
    # preserve every argument/state/container alias in the encoded graph.
    comparison = RestoredPreparedBatchData(
        batch=replace(batch, current_call=replace(call, audit=None)),
        accumulator=data.accumulator,
        calls=calls,
        audit=prepared.portable_audit,
        dialect=ctx.tool_contract_dialect,
    )
    if encode_restored_prepared_batch_data(comparison, env_ref_names=names) != prepared.prepared_batch_json:
        raise AuditIntegrityError("metadata completion changed its original detached prepared graph")
    audit = call.audit
    before = len(ctx.recorder.invocations)
    batch.all_cache_hits = False

    def version_after(result: object) -> int:
        if type(result) is not ToolResult:
            raise AuditIntegrityError("metadata computation replaced its owned result")
        return result.updated_state.version

    def error_payload(error: ToolArgumentError) -> dict[str, JsonValue]:
        payload = owned_json(arg_error_payload(error, call.tool_name))
        if type(payload) is not dict:
            raise AuditIntegrityError("metadata argument failure lost its owned JSON payload")
        return payload

    try:
        outcome = await dispatch_with_audit(
            recorder=ctx.recorder,
            audit=audit,
            do_dispatch=compute,
            version_after_provider=version_after,
            arg_error_payload_factory=error_payload,
        )
    except ToolArgumentError as error:
        raise MetadataPreparationRefused("metadata arguments do not have an admitted pure result") from error
    result = outcome.result
    if type(result) is not ToolResult or len(ctx.recorder.invocations) != before + 1:
        raise AuditIntegrityError("metadata completion lost its actual single result and invocation")
    receipt = capture_tool_receipt(
        effect_key=f"memory:{call.tool_call.id}",
        invocation=ctx.recorder.invocations[-1],
        result=result,
        pre_state=call.pre_state,
        env_ref_names=names,
    )
    call.memory_result = result
    call.owner_tool_receipt_json = encode_retained_tool_receipt(receipt, env_ref_names=names)
    call.phase = "application_pending"
    memory_json = encode_memory_completion_batch_data(
        batch=batch, accumulator=data.accumulator, calls=calls, dialect=ctx.tool_contract_dialect, env_ref_names=names
    )
    batch.state, batch.last_validation, batch.last_runtime_preflight = completed_tool_state(
        result,
        state=batch.state,
        last_validation=batch.last_validation,
        last_runtime_preflight=batch.last_runtime_preflight,
        unapplied=False,
    )
    batch.mutation_success_observed = record_completed_tool_anchor(
        result,
        tracker=ctx.anti_anchor,
        tool_name=call.tool_name,
        arguments_hash=audit.arguments_hash,
        version_before=call.pre_version,
        mutation_success_observed=batch.mutation_success_observed,
        unapplied=False,
    )
    _append_continuation_tool_outcome(
        batch, call, response=result, error_class=None, error_category=None, error_message=None, post_version=batch.state.version
    )
    serialized = serialize_tool_result(result)
    producers = ctx.message_producers
    if producers is None:
        raise AuditIntegrityError("metadata completion lost its original-source message producer")
    if type(prepared) is PreparedMetadataToolCall:
        producers.register_prepared_metadata_result_message(prepared=prepared, result=result, serialized=serialized)
    elif type(prepared) is PreparedPipelineToolCall:
        producers.register_prepared_pipeline_result_message(prepared=prepared, result=result, serialized=serialized)
    else:
        raise AuditIntegrityError("memory result replaced its original nominal preparation")
    batch.turn_has_mutation = True
    call.phase = "applied"
    batch.next_tool_index = 1
    batch.current_call = None
    batch.phase = "dispatch_ready"
    return PreparedMemoryToolCompletion(prepared, result, receipt, memory_json)
