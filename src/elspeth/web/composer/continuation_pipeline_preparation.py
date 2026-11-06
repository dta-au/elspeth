"""Detached V4 preparation of one original planner-authored pipeline value.

This path computes the existing shared candidate only. It cannot publish a
graph, persist a blob, allocate a proposal or issue a tool. The session owner
must admit the complete tool/P4 tail before adopting the detached value.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_metadata_preparation import (
    MetadataPreparationRefused,
    PreparedMemoryToolCall,
    PreparedMemoryToolCompletion,
    complete_prepared_memory_tool,
    prepare_memory_tool_call,
)
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
from elspeth.web.composer.tool_batch import _finalize_complete_set_pipeline_candidate
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.composer.tools._registry import resolve_tool_effects
from elspeth.web.composer.tools.declarations import GRAPH_EFFECTS
from elspeth.web.composer.tools.sessions import SetPipelineCandidate
from elspeth.web.credential_guard import require_no_credential_material_for_tool
from elspeth.web.interpretation_state import interpretation_sites


@dataclass(frozen=True, slots=True)
class PreparedPipelineToolCall(PreparedMemoryToolCall):
    """One original V4 set_pipeline call on independent mutable owners."""


@dataclass(frozen=True, slots=True)
class PreparedPipelineToolCompletion(PreparedMemoryToolCompletion):
    prepared: PreparedPipelineToolCall


async def prepare_pipeline_tool_call(
    *,
    runtime: RestoredContinuationRuntime,
    original_runtime: RestoredContinuationRuntime,
    call_model: _CallModelOutcome,
    cancellation_requested: asyncio.Event,
) -> PreparedPipelineToolCall:
    prepared = await prepare_memory_tool_call(
        runtime=runtime,
        original_runtime=original_runtime,
        call_model=call_model,
        cancellation_requested=cancellation_requested,
        tool_name="set_pipeline",
        prepared_type=PreparedPipelineToolCall,
    )
    if type(prepared) is not PreparedPipelineToolCall:
        raise AuditIntegrityError("pipeline preparation replaced its nominal value")
    arguments = prepared.call.arguments
    if arguments is None or resolve_tool_effects("set_pipeline", arguments) != GRAPH_EFFECTS:
        raise MetadataPreparationRefused("pipeline preparation cannot admit a blob or other non-graph effect")
    return prepared


async def complete_prepared_pipeline_tool(prepared: PreparedPipelineToolCall) -> PreparedPipelineToolCompletion:
    if type(prepared) is not PreparedPipelineToolCall:
        raise TypeError("pipeline completion requires its original prepared DATA")

    async def compute() -> ToolResult:
        data = prepared.runtime.graph.batch
        if type(data) is not RestoredBatchData:
            raise AuditIntegrityError("pipeline completion lost its original detached batch")
        ctx, call, batch = prepared.context, prepared.call, data.batch
        arguments = call.arguments
        if arguments is None or resolve_tool_effects("set_pipeline", arguments) != GRAPH_EFFECTS:
            raise MetadataPreparationRefused("pipeline completion cannot admit a blob or other non-graph effect")
        require_no_credential_material_for_tool(
            "set_pipeline", arguments, surface="composer_tool_arguments", credential_state=call.pre_state
        )
        prior_validation = batch.last_validation
        if prior_validation is None:
            prior_validation = ctx.policy_catalog.validate_composition_state(call.pre_state).validation
        candidate_context = ToolContext(
            catalog=ctx.policy_catalog,
            plugin_snapshot=ctx.plugin_snapshot,
            data_dir=ctx.custody.data_dir,
            require_data_dir_for_paths=True,
            session_engine=ctx.custody.session_engine,
            session_id=ctx.session_id,
            session_operation_context=ctx.session_operation_context,
            session_operation_authority=ctx.session_operation_authority,
            secret_service=ctx.custody.secret_service,
            secret_wiring_policy=ctx.custody.secret_wiring_policy,
            user_id=ctx.user_id,
            current_validation=prior_validation,
            max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
            user_message_id=ctx.user_message_id,
            user_message_content=ctx.user_message_content,
            composer_model_identifier=ctx.provenance.model_identifier,
            composer_model_version=batch.provider_model_version,
            composer_provider=ctx.provenance.provider or "unknown",
            composer_skill_hash=ctx.provenance.skill_hash,
            tool_arguments_hash=prepared.original_audit.binding_arguments_hash,
            mutation_ceiling=ctx.mutation_ceiling,
        )
        finalization = await _finalize_complete_set_pipeline_candidate(
            arguments,
            call.pre_state,
            candidate_context,
            plugin_snapshot=ctx.plugin_snapshot,
            policy_catalog=ctx.policy_catalog,
        )
        candidate = finalization.candidate
        if type(candidate) is not SetPipelineCandidate:
            raise AuditIntegrityError("pipeline finalization replaced its original owned candidate")
        result = candidate.result
        # Argument/audit rebinding, blob persistence, interpretation work and
        # runtime preflight each need their own complete tail. This first
        # graph-only capability does not enter those branches.
        if (
            finalization.changed
            or finalization.arguments != arguments
            or candidate.prepared_inline_blob is not None
            or not candidate.acceptable
            or result.updated_state.version != call.pre_version + 1
            or result.plugin_schemas is not None
            or interpretation_sites(result.updated_state)
        ):
            raise MetadataPreparationRefused("pipeline candidate requires an unsupported result or additional effect tail")
        return result

    completed = await complete_prepared_memory_tool(prepared, compute=compute)
    return PreparedPipelineToolCompletion(prepared, completed.result, completed.receipt, completed.memory_completion_json)
