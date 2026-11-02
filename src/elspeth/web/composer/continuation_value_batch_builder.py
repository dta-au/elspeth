"""Prepare complete original V4 value batches without adopting user effects."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    decode_prepared_batch_data,
    encode_memory_completion_batch_data,
    encode_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_p4_batch import encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_prepared_p4 import encode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    _capture_owners,
    build_runtime_tool_batch_context,
    capture_metadata_preparation_runtime,
    restore_metadata_preparation_runtime,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_receipts import capture_tool_receipt, encode_retained_tool_receipt
from elspeth.web.composer.continuation_value_batch_computation import (
    VALUE_BATCH_GRAPH_TOOLS,
    VALUE_BATCH_READ_TOOLS,
    observe_value_batch_computation,
    original_value_batch_computation,
    original_value_batch_result_source,
)
from elspeth.web.composer.continuation_value_batch_preparation import (
    PreparedValueBatch,
    PreparedValueBatchCall,
    PreparedValueBatchStep,
    ValueBatchPreparationRefused,
)
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tool_batch import (
    ContinuationCallState,
    _execute_continuation_call_live,
    _finish_continuation_batch_live,
    _prepare_continuation_call_live,
)
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.turn_audit import prepare_continuation_turn_audit, prepare_turn_audit_evidence, prepare_turn_audit_rows
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Value
    from elspeth.web.composer.continuation_preview_dispatch import IssuedPreviewDispatch
    from elspeth.web.composer.tool_batch import ToolBatchContext


async def _capture(
    runtime: RestoredContinuationRuntime, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> CapturedContinuationRuntime:
    value = await capture_metadata_preparation_runtime(runtime, selected=selected, inventory=inventory)
    if value is None:
        raise ValueBatchPreparationRefused("value batch exceeds the unchanged complete document/evidence bound")
    return value


def _adopt_batch_state(runtime: RestoredContinuationRuntime) -> RestoredBatchData:
    data = runtime.graph.batch
    if type(data) is not RestoredBatchData:
        raise AuditIntegrityError("value batch lost its actual coupled owners")
    live, batch = runtime.live, data.batch
    live.state, live.last_validation, live.last_runtime_preflight = batch.state, batch.last_validation, batch.last_runtime_preflight
    live.advisor_calls_used = batch.advisor_calls_used
    return data


async def _prepare_one(
    owner: ContinuationLivePhases,
    *,
    runtime: RestoredContinuationRuntime,
    call_model: _CallModelOutcome,
    context: ToolBatchContext,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    index: int,
) -> PreparedValueBatchStep:
    from elspeth.web.composer.audit import DispatchAudit

    data = _adopt_batch_state(runtime)
    batch, acc = data.batch, data.accumulator
    if batch.phase != "call_ready" or batch.current_call is not None or batch.next_tool_index != index:
        raise AuditIntegrityError("value batch changed its original ordered preparation boundary")
    call = ContinuationCallState(index, data.calls[index], batch.state, batch.state.version)
    batch.current_call, batch.phase = call, "call_active"
    before_invocations = len(context.recorder.invocations)
    status = await _prepare_continuation_call_live(
        call_model=call_model, ctx=context, llm_messages=runtime.live.llm_messages, batch=batch, call=call
    )
    if status == "stop":
        raise ValueBatchPreparationRefused("value batch stopped before its complete prepared tail")
    if status == "applied":
        if (
            len(context.recorder.invocations) != before_invocations + 1
            or context.recorder.invocations[-1].status is not ComposerToolStatus.ARG_ERROR
            or len(batch.tool_outcomes) != index + 1
            or batch.tool_outcomes[-1].call is not call.tool_call
        ):
            raise ValueBatchPreparationRefused("value preparation requires an unsupported early-result owner")
        runtime.materialization.register_prepared_value_batch_preparation_error(runtime=runtime, call_model=call_model, index=index)
        batch.current_call = None
        batch.next_tool_index = index + 1
        batch.phase = "dispatch_ready" if index + 1 == len(data.calls) else "call_ready"
        _adopt_batch_state(runtime)
        return PreparedValueBatchStep(index, None, await _capture(runtime, selected, inventory), None)
    if status != "prepared" or type(call.audit) is not DispatchAudit:
        raise AuditIntegrityError("value preparation lost its original live dispatch audit")
    encoded = encode_prepared_batch_data(
        batch=batch, accumulator=acc, calls=data.calls, dialect=context.tool_contract_dialect, env_ref_names=inventory.names
    )
    portable = decode_prepared_batch_data(encoded, env_ref_names=inventory.names).audit
    prepared = PreparedValueBatchCall(call_model, context, runtime, call, encoded, call.audit, replace(call.audit), portable)
    issued_runtime = replace(
        runtime,
        graph=runtime.graph.with_selected_dispatch(
            capture_dispatch_audit(call.audit, env_ref_names=inventory.names), context.tool_contract_dialect
        ),
    )
    issued = await _capture(issued_runtime, selected, inventory)
    before = replace(batch, tool_outcomes=list(batch.tool_outcomes))
    observed = await observe_value_batch_computation(prepared)
    await _execute_continuation_call_live(
        call_model=call_model,
        ctx=context,
        acc=acc,
        llm_messages=runtime.live.llm_messages,
        batch=batch,
        call=call,
        value_computation=observed,
    )
    runtime.materialization.register_prepared_value_batch_messages(prepared=prepared, computation=observed)
    if (
        len(context.recorder.invocations) != before_invocations + 1
        or len(batch.tool_outcomes) != index + 1
        or batch.tool_outcomes[-1].call is not call.tool_call
        or batch.stop_issued_batch
        or acc.authority_revocation is not None
    ):
        raise ValueBatchPreparationRefused("value batch cannot adopt an incomplete or revoked prefix")
    outcome = batch.tool_outcomes[-1]
    result = outcome.response
    invocation = context.recorder.invocations[-1]
    if invocation.status is ComposerToolStatus.ARG_ERROR:
        original_prepared, _value, _admitted, error, _cached = original_value_batch_result_source(observed)
        if (
            original_prepared is not prepared
            or error is None
            or result is not None
            or invocation.error_class != type(error).__name__
            or invocation.error_message != str(error.args[0] if error.args else "ToolArgumentError")
            or invocation.version_after is not None
            or batch.state is not call.pre_state
        ):
            raise AuditIntegrityError("value argument refusal changed its original no-effect error source")
        # The computation and ordinary error audit were detached preparation.
        # No SQL issue or user effect occurred, and no ToolResult exists to
        # encode as a successful memory completion. Retain that real refusal.
        batch.current_call = None
        batch.next_tool_index = index + 1
        batch.phase = "dispatch_ready" if index + 1 == len(data.calls) else "call_ready"
        _adopt_batch_state(runtime)
        return PreparedValueBatchStep(index, None, await _capture(runtime, selected, inventory), None)
    if result is not None and type(result) is not ToolResult:
        raise AuditIntegrityError("value completion lost its actual nominal tool result")
    receipt = capture_tool_receipt(
        effect_key=f"memory:{call.tool_call.id}",
        invocation=invocation,
        result=result,
        pre_state=call.pre_state,
        env_ref_names=inventory.names,
    )
    memory_call = replace(
        call,
        phase="application_pending",
        memory_result=result,
        owner_tool_receipt_json=encode_retained_tool_receipt(receipt, env_ref_names=inventory.names),
    )
    memory_batch = replace(before, current_call=memory_call, all_cache_hits=batch.all_cache_hits)
    memory_json = encode_memory_completion_batch_data(
        batch=memory_batch, accumulator=acc, calls=data.calls, dialect=context.tool_contract_dialect, env_ref_names=inventory.names
    )
    payload = memory_json.encode("utf-8")
    memory = ProspectiveContinuationChunk(
        f"value-memory:{sha256(payload).hexdigest()}", payload, "composer.memory-completion-batch-data.v1"
    )
    batch.current_call = None
    batch.next_tool_index = index + 1
    batch.phase = "dispatch_ready" if index + 1 == len(data.calls) else "call_ready"
    _adopt_batch_state(runtime)
    _original, computed = original_value_batch_computation(observed)
    chunks = () if computed is None else computed.source_chunks
    return PreparedValueBatchStep(index, issued, await _capture(runtime, selected, inventory), memory, chunks)


async def build_prepared_value_batch(
    owner: ContinuationLivePhases,
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> PreparedValueBatch:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if (
        type(owner) is not ContinuationLivePhases
        or type(runtime) is not RestoredContinuationRuntime
        or runtime.source.selected is not selected
        or type(runtime.source.policy) is not ContinuationAdmissionPolicyV4
        or inventory.running is not owner.work.running
        or len(selected.stack.frames) != 2
        or type(selected.stack.frames[0]) is not ComposeFrame
        or type(selected.stack.frames[1]) is not ToolBatchFrame
        or selected.stack.active_action_json is not None
        or selected.stack.active_action_source is not None
        or runtime.live.advisor_repair_context_introduced
    ):
        raise ValueBatchPreparationRefused("value batch requires its original ordinary V4 PRIMARY source")
    original = runtime.graph.batch
    if (
        type(original) is not RestoredBatchData
        or original.batch.phase != "call_ready"
        or original.batch.next_tool_index != 0
        or original.batch.current_call is not None
        or original.batch.tool_outcomes
        or not original.calls
        or any(call.function.name not in VALUE_BATCH_READ_TOOLS | VALUE_BATCH_GRAPH_TOOLS for call in original.calls)
    ):
        raise ValueBatchPreparationRefused("value batch requires a complete supported unissued provider batch")
    sessions = owner.service._sessions_service
    if type(sessions) is not SessionServiceImpl:
        raise ValueBatchPreparationRefused("value batch requires its actual session publication owner")
    provider = ProviderInvocationOwner(service=sessions, required_work=owner.work)
    detached = await restore_metadata_preparation_runtime(
        service=owner.service,
        authority=owner.authority,
        selected=selected,
        inventory=inventory,
        required_work=owner.work,
        provider_owner=provider,
    )
    data = _adopt_batch_state(detached)
    if (
        detached is runtime
        or detached.live is runtime.live
        or detached.materialization is runtime.materialization
        or data.batch is original.batch
        or data.accumulator is original.accumulator
        or data.batch.tool_outcomes is original.batch.tool_outcomes
        or detached.live.recorder is runtime.live.recorder
        or detached.live.llm_messages is runtime.live.llm_messages
        or detached.live.turn_evidence is runtime.live.turn_evidence
        or detached.live.discovery_cache is runtime.live.discovery_cache
        or detached.live.anti_anchor is runtime.live.anti_anchor
    ):
        raise AuditIntegrityError("value batch reused an original mutable owner")
    phase = ContinuationLivePhases(owner.service, owner.authority, owner.work, provider, owner.lease, owner.progress, runtime=detached)
    primary = await phase._primary_for_phase(detached, inventory, selected)
    context = await build_runtime_tool_batch_context(detached, cancellation_requested=owner.cancellation_requested)
    steps = []
    for index in range(len(data.calls)):
        steps.append(
            await _prepare_one(
                owner, runtime=detached, call_model=primary.outcome, context=context, selected=selected, inventory=inventory, index=index
            )
        )
    pending_runtime, pending_chunk, prepared = _prepare_pending_value_batch(
        owner, detached=detached, call_model=primary.outcome, context=context, inventory=inventory
    )
    pending_capture = await _capture(pending_runtime, selected, inventory)
    return PreparedValueBatch(selected, inventory, data.calls, tuple(steps), pending_capture, pending_chunk, prepared)


def _prepare_pending_value_batch(
    owner: ContinuationLivePhases,
    *,
    detached: RestoredContinuationRuntime,
    call_model: _CallModelOutcome,
    context: ToolBatchContext,
    inventory: CurrentContinuationEnvInventory,
) -> tuple[RestoredContinuationRuntime, PrivateChunk, PreparedP4Value]:
    """The unchanged original P4 projection, with no publication or SQL."""
    data = _adopt_batch_state(detached)
    dispatch, _passes = _finish_continuation_batch_live(call_model=call_model, ctx=context, acc=data.accumulator, batch=data.batch)
    live, batch = detached.live, data.batch
    operation = inventory.running.session_operation_context
    rows = prepare_turn_audit_rows(
        redaction_telemetry=owner.service._redaction_telemetry,
        tool_outcomes=dispatch.tool_outcomes,
        base_state=batch.entry_state,
        decoded_args_by_call_id=dispatch.decoded_args_by_call_id,
        private_authorings=dispatch.private_authorings,
        crash_pending=False,
        session_id=str(inventory.running.claim.session_id),
        session_operation_context=operation,
        ingress=detached.bindings.ingress,
        chat_ingress_inputs=None if detached.bindings.chat_ingress_inputs is None else list(detached.bindings.chat_ingress_inputs),
        turn_evidence=live.turn_evidence,
    )
    evidence = prepare_turn_audit_evidence(
        tool_outcomes=dispatch.tool_outcomes,
        private_authorings=dispatch.private_authorings,
        private_invocations=dispatch.private_invocations,
        session_id=str(inventory.running.claim.session_id),
        session_operation_context=operation,
    )
    prepared = prepare_continuation_turn_audit(
        rows=rows,
        evidence=evidence,
        session_operation_context=operation,
        assistant_message=dispatch.assistant_message,
        raw_assistant_content=dispatch.raw_assistant_content,
        mutation_ceiling=detached.source.selected.stack.mutation_ceiling.materialize(),
        authority_revocation=dispatch.authority_revocation,
        tool_outcomes=dispatch.tool_outcomes,
        private_invocations=dispatch.private_invocations,
        private_authorings=dispatch.private_authorings,
        withheld_replies=live.recorder.withheld_replies[detached.drained.withheld_replies :],
        current_state_id=live.current_state_id,
        assistant_row_uses_current_dispatch=True,
        crash_pending=False,
        env_ref_names=inventory.names,
    )
    members = prepared_p4_members(prepared, env_ref_names=inventory.names)
    if (
        tuple(call["id"] for call in members.redacted_calls) != tuple(call.id for call in data.calls)
        or tuple(row.tool_call_id for row in members.redacted_rows) != tuple(call.id for call in data.calls)
        or members.invocations != dispatch.private_invocations
        or len(dispatch.tool_outcomes) != len(data.calls)
    ):
        raise AuditIntegrityError("value P4 changed its actual complete outcome/audit cohort")
    for expected, row in zip(members.invocations, members.redacted_rows, strict=True):
        actual = tuple(value for value in live.recorder.invocations if value.tool_call_id == expected.tool_call_id)
        if len(actual) != 1 or actual[0] != expected:
            raise AuditIntegrityError("value redaction changed its actual original invocation")
        if row.visible_event is not None:
            previous = tuple(event for invocation, event in live.recorder.visible_tool_events if invocation is actual[0])
            if previous and previous != (row.visible_event,):
                raise AuditIntegrityError("value redaction replaced its actual visible event")
            if not previous:
                live.recorder.record_visible_tool_event(actual[0], row.visible_event)
    pending = replace(batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared, env_ref_names=inventory.names))
    pending_chunk = encode_p4_pending_batch_chunk(
        batch=pending, accumulator=data.accumulator, calls=data.calls, env_ref_names=inventory.names
    )
    pending_runtime = replace(detached, graph=detached.graph.with_batch(RestoredBatchData(pending, data.accumulator, data.calls)))
    return pending_runtime, pending_chunk, prepared


async def adopt_issued_preview_batch(*, prepared: PreparedValueBatchCall, permit: IssuedPreviewDispatch) -> None:
    """Run original native result/audit handling without invoking its producer."""
    from elspeth.web.composer.continuation_preview_dispatch import (
        adopt_preview_issued_message_source,
        original_adopted_preview_result,
        require_preview_adoption,
    )

    runtime, context = prepared.runtime, prepared.context
    data = _adopt_batch_state(runtime)
    require_preview_adoption(
        permit, call_model=prepared.call_model, ctx=context, acc=data.accumulator, batch=data.batch, call=prepared.call
    )
    adopt_preview_issued_message_source(permit, prepared=prepared)
    before = len(context.recorder.invocations)
    await _execute_continuation_call_live(
        call_model=prepared.call_model,
        ctx=context,
        acc=data.accumulator,
        llm_messages=runtime.live.llm_messages,
        batch=data.batch,
        call=prepared.call,
        issued_preview=permit,
    )
    original = original_adopted_preview_result(permit, prepared=prepared)
    if (
        len(data.calls) != 1
        or len(context.recorder.invocations) != before + 1
        or len(data.batch.tool_outcomes) != 1
        or data.batch.tool_outcomes[0].call is not prepared.call.tool_call
        or data.batch.tool_outcomes[0].response != original.to_tool_result()
        or data.batch.stop_issued_batch
        or data.accumulator.authority_revocation is not None
    ):
        raise AuditIntegrityError("issued preview changed its actual complete native result prefix")
    runtime.materialization.register_issued_preview_result_message(prepared=prepared, permit=permit)
    data.batch.current_call = None
    data.batch.next_tool_index = 1
    data.batch.phase = "dispatch_ready"
    _adopt_batch_state(runtime)


def build_prepared_issued_preview_batch(
    owner: ContinuationLivePhases,
    *,
    prepared: PreparedValueBatchCall,
    permit: IssuedPreviewDispatch,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> PreparedValueBatch:
    """Capture full-tail DATA from the actual issued root and returned result.

    The physical result writer independently reproves the current source and
    reserves every prefix before writing. Capture retains its original fence
    and message-source SQL reproofs, so callers must finish it before opening
    the result writer's transaction. It performs no new resource producer,
    ordinary clock renewal or user publication.
    """
    from elspeth.web.composer.continuation_preview_dispatch import original_adopted_preview_result

    original_adopted_preview_result(permit, prepared=prepared)
    if (
        runtime.source.selected is not selected
        or type(runtime.source.policy) is not ContinuationAdmissionPolicyV4
        or inventory.running is not owner.work.running
        or runtime.bindings.session_operation_context is not prepared.runtime.bindings.session_operation_context
        or selected.stack.frames[-1].frame_kind != "tool_batch"
    ):
        raise AuditIntegrityError("issued preview tail changed its actual current runtime source")
    top = selected.stack.frames[-1]
    if type(top) is not ToolBatchFrame or top.phase != "effect_reconciling" or top.next_tool_index != 0:
        raise AuditIntegrityError("issued preview tail requires its actual already-issued frame")
    actual = prepared.runtime
    data = _adopt_batch_state(actual)
    if len(data.calls) != 1 or data.batch.phase != "dispatch_ready" or data.batch.current_call is not None:
        raise AuditIntegrityError("issued preview tail lost its complete original native adoption")
    completed = _capture_owners(actual, source=runtime.source, inventory=inventory)
    pending_runtime, pending_chunk, p4 = _prepare_pending_value_batch(
        owner, detached=actual, call_model=prepared.call_model, context=prepared.context, inventory=inventory
    )
    pending_capture = _capture_owners(pending_runtime, source=runtime.source, inventory=inventory)
    step = PreparedValueBatchStep(0, None, completed, None)
    return PreparedValueBatch(selected, inventory, data.calls, (step,), pending_capture, pending_chunk, p4)
