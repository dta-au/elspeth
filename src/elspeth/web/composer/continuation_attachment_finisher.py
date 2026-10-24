"""Apply one actually current-read attachment result through the original B2 owner.

This live projection is not a durable receipt or current SQL grant. Its caller
must publish the successor with the exact original selected CAS. Unpublished
projection objects are never an already-applied retry cache.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_attachment_result import read_attachment_result
from elspeth.web.composer.continuation_batch_data import (
    RestoredBatchData,
    RestoredPreparedBatchData,
    encode_restored_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import encode_continuation_response
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit, re_admit_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence, capture_turn_evidence
from elspeth.web.composer.continuation_evidence_restore import _require_current_reader_service
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_receipts import RetainedToolReceipt, capture_tool_receipt, require_receipt_result
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tool_batch import ToolBatchContext, _execute_continuation_call_live
from elspeth.web.composer.tools import ToolResult
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.continuation_attachment_result import AttachmentResultCheckpoint, _result, require_attachment_read_audit


@dataclass(frozen=True, slots=True)
class CompletedAttachmentProjection:
    source: AttachmentResultCheckpoint
    before: TurnEvidenceSnapshot
    after: TurnEvidenceSnapshot
    data: RestoredBatchData
    receipt: RetainedToolReceipt


async def finish_current_attachment_result(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    ctx: ToolBatchContext,
    call_model: _CallModelOutcome,
    restored: RestoredTurnEvidence,
) -> CompletedAttachmentProjection:
    if type(ctx) is not ToolBatchContext or type(restored) is not RestoredTurnEvidence:
        raise AuditIntegrityError("attachment finishing requires its actual current runtime owners")
    _require_current_reader_service(service, authority)
    if (
        required_work.running is not inventory.running
        or ctx.session_operation_context is not inventory.running.session_operation_context
        or ctx.custody.session_engine is not authority._engine
        or ctx.turn_sessions_service is not service._require_sessions_service()
        or ctx.turn_evidence is not restored.evidence
        or ctx.recorder is not restored.recorder
        or ctx.message_producers is None
        or ctx.message_producers.service is not service
        or ctx.message_producers.authority is not authority
        or ctx.message_producers.inventory.running is not inventory.running
        or ctx.message_producers.inventory.names != inventory.names
        or ctx.message_producers.selected.stack.logical != selected.stack.logical
        or ctx.message_producers.selected.stack.mutation_ceiling != selected.stack.mutation_ceiling
        or ctx.message_producers.selected.stack.policy_hash != selected.stack.policy_hash
        or ctx.user_id != restored.evidence._continuation_actor_user_id
        or ctx.session_id != restored.evidence.session_id
        or ctx.session_id != str(inventory.running.claim.session_id)
    ):
        raise AuditIntegrityError("attachment finishing crossed current runtime/custody/evidence owners")
    data = restored.batch
    if data is None or restored.selected_audit is None or restored.selected_dialect is None:
        raise AuditIntegrityError("attachment finishing lost its complete shared prepared audit graph")
    prepared = RestoredPreparedBatchData(data.batch, data.accumulator, data.calls, restored.selected_audit, restored.selected_dialect)
    if ctx.actor != prepared.audit.actor or ctx.tool_contract_dialect is not prepared.dialect:
        raise AuditIntegrityError("attachment finishing changed its actual current actor/dialect")
    call = data.batch.current_call
    if call is None or call.phase != "prepared" or call.tool_name not in ("read_attachment", "get_blob_content"):
        raise AuditIntegrityError("attachment finishing has no exact unconsumed prepared call")
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame or frame.phase != "result_retained" or frame.next_tool_index != call.index:
        raise AuditIntegrityError("attachment finishing selected another actual call/cursor")
    response = encode_continuation_response(call_model.completion, audit_call=call_model.audit_call)
    if response.reference != frame.response or call_model.completion.tool_batch.calls != data.calls:
        raise AuditIntegrityError("attachment finishing changed its complete original provider response")
    ctx.message_producers.require_context(session_id=ctx.session_id, context=ctx.session_operation_context, actor_user_id=ctx.user_id)
    from elspeth.web.composer.continuation_runtime import _registered_read
    from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource

    # The old warm registry retains original message identities; current scope
    # comes from this complete latest SQL source read, never registry rebinding.
    await _registered_read(
        required_work,
        ContinuationRuntimeSource,
        lambda: authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory),
    )
    before = capture_turn_evidence(
        restored.evidence,
        restored.messages,
        logical=selected.stack.logical,
        recorder=restored.recorder,
        drained=restored.drained,
        env_ref_names=inventory.names,
        data=prepared,
        materialization=ctx.message_producers,
    )
    from elspeth.web.composer.continuation_evidence_application_data import require_evidence_application_links

    before_graph = require_evidence_application_links(before, env_ref_names=inventory.names)
    if before_graph is None or before_graph.selected_audit is None or before_graph.selected_dialect is None:
        raise AuditIntegrityError("attachment finishing lost its complete original prepared capture")
    selected_json = encode_restored_prepared_batch_data(
        RestoredPreparedBatchData(
            before_graph.selected.batch,
            before_graph.selected.accumulator,
            before_graph.selected.calls,
            before_graph.selected_audit,
            before_graph.selected_dialect,
        ),
        env_ref_names=inventory.names,
    )
    current = await read_attachment_result(authority=authority, selected=selected, inventory=inventory, required_work=required_work)
    if not current.history_applied or encode_restored_prepared_batch_data(current.prepared, env_ref_names=inventory.names) != selected_json:
        raise AuditIntegrityError("attachment finishing differs from its complete current SQL source graph")
    if current.read_audit is None:
        raise AuditIntegrityError("historical attachment result lacks its actual completed-read timing observation")
    require_attachment_read_audit(prepared, current.read_audit, names=inventory.names)
    # Re-admit the exact nonmutating result against this real shared pre-state;
    # no equality-based merging of the separately loaded graph is performed.
    result = _result(current.retained, prepared, inventory.names)
    bound_source = replace(current, prepared=prepared, result=result)
    if call.audit is None:
        if call.audit_arguments is None:
            raise AuditIntegrityError("attachment finishing lost its actual audit argument owner")
        call.audit = re_admit_dispatch_audit(
            current.read_audit,
            selected_call=call.tool_call,
            pre_state=call.pre_state,
            audit_arguments=call.audit_arguments,
            actor=ctx.actor,
            strict_sent=call.wire.strict_sent,
            wire_conformant=call.wire.wire_conformant,
            env_ref_names=inventory.names,
        )
    else:
        observed = capture_dispatch_audit(call.audit, env_ref_names=inventory.names)
        require_attachment_read_audit(prepared, observed, names=inventory.names)
        if observed.elapsed_active_ns < current.read_audit.elapsed_active_ns:
            raise AuditIntegrityError("live attachment timer predates its actual retained read completion")
    # Existing live audits retain their original object/timer. Complete prepared
    # admission above checked their actor/arguments/start binding independently.
    original_invocations = restored.recorder.invocations
    original_count = len(data.batch.tool_outcomes)
    await _execute_continuation_call_live(
        call_model=call_model,
        ctx=ctx,
        acc=data.accumulator,
        llm_messages=restored.messages,
        batch=data.batch,
        call=call,
        retained_attachment=bound_source,
    )
    invocations = restored.recorder.invocations
    if (
        len(invocations) != len(original_invocations) + 1
        or any(old is not new for old, new in zip(original_invocations, invocations, strict=False))
        or len(data.batch.tool_outcomes) != original_count + 1
    ):
        raise AuditIntegrityError("attachment finisher lost its exact one original invocation/outcome")
    outcome = data.batch.tool_outcomes[-1]
    completed_result = outcome.response
    if type(completed_result) is not ToolResult:
        raise AuditIntegrityError("attachment finisher lost its actual completed nominal ToolResult")
    receipt = capture_tool_receipt(
        effect_key=current.effect_key,
        invocation=invocations[-1],
        result=completed_result,
        pre_state=call.pre_state,
        env_ref_names=inventory.names,
    )
    require_receipt_result(
        receipt, selected_call=call.tool_call, pre_state=call.pre_state, result=completed_result, env_ref_names=inventory.names
    )
    data.batch.next_tool_index += 1
    data.batch.current_call = None
    data.batch.phase = "dispatch_ready" if data.batch.stop_issued_batch or data.batch.next_tool_index == len(data.calls) else "call_ready"
    after = capture_turn_evidence(
        restored.evidence,
        restored.messages,
        logical=selected.stack.logical,
        recorder=restored.recorder,
        drained=restored.drained,
        env_ref_names=inventory.names,
        data=data,
        materialization=ctx.message_producers,
    )
    from elspeth.web.composer.continuation_attachment_application import require_attachment_projection

    require_attachment_projection(
        before=before,
        after=after,
        prepared_source=current.prepared,
        retained_result=current.retained,
        read_audit=current.read_audit,
        effect_key=current.effect_key,
        receipt=receipt,
        response=response,
        env_ref_names=inventory.names,
    )
    return CompletedAttachmentProjection(current, before, after, data, receipt)
