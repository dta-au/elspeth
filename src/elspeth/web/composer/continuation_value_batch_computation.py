"""Once-computed original value results for the unchanged audited tool worker."""

from __future__ import annotations

from dataclasses import dataclass
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, RestoredPreparedBatchData, encode_restored_prepared_batch_data
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_tool_batch import encode_tool_result_value
from elspeth.web.composer.continuation_value_batch_preparation import (
    PreparedValueBatchCall,
    ValueBatchComputation,
    ValueBatchPreparationRefused,
)
from elspeth.web.composer.discovery_cache import CachedDiscoveryPayload, make_cache_key
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tool_batch import (
    BatchAccumulator,
    ContinuationBatchState,
    ContinuationCallState,
    ToolBatchContext,
    _finalize_complete_set_pipeline_candidate,
    _finalize_completed_incremental_mutation,
)
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.composer.tools._registry import continuation_declaration, response_contract_for
from elspeth.web.composer.tools.declarations import GRAPH_EFFECTS, ToolKind
from elspeth.web.composer.tools.sessions import SetPipelineCandidate
from elspeth.web.credential_guard import require_no_credential_material_for_tool
from elspeth.web.interpretation_state import interpretation_sites

# These are deliberate value-owner capabilities, not a replacement registry.
# Every call still resolves its actual declaration/kind/effects and handler.
VALUE_BATCH_READ_TOOLS = frozenset(
    (
        "list_sources",
        "list_transforms",
        "list_sinks",
        "list_models",
        "get_plugin_schema",
        "get_expression_grammar",
        "explain_validation_error",
        "get_plugin_assistance",
        "get_audit_info",
        "get_pipeline_state",
        "get_complete_pipeline_value",
        "diff_pipeline",
    )
)
VALUE_BATCH_GRAPH_TOOLS = frozenset(
    (
        "set_source",
        "patch_source_options",
        "clear_source",
        "set_pipeline",
        "upsert_node",
        "splice_transform",
        "upsert_edge",
        "remove_node",
        "remove_edge",
        "set_metadata",
        "patch_node_options",
        "set_output",
        "remove_output",
        "patch_output_options",
    )
)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ObservedValueBatchComputation:
    """Process-local observation; a public equal object is never its source."""


@dataclass(slots=True)
class _OriginalComputation:
    prepared: PreparedValueBatchCall
    value: ValueBatchComputation | None
    admitted: AdmittedDiscoveryResult | None
    error: ToolArgumentError | None
    cached: CachedDiscoveryPayload | None
    result_json: str | None
    consumed: bool = False


_OBSERVATIONS: WeakKeyDictionary[ObservedValueBatchComputation, _OriginalComputation] = WeakKeyDictionary()


def _result_json(prepared: PreparedValueBatchCall, result: ToolResult) -> str:
    value = encode_tool_result_value(prepared.call.tool_name, result, env_ref_names=prepared.runtime.env_ref_names)
    return canonical_json(value.model_dump(mode="json"))


def _require_original_call(prepared: PreparedValueBatchCall) -> RestoredBatchData:
    if type(prepared) is not PreparedValueBatchCall:
        raise TypeError("value computation requires its actual prepared call")
    data = prepared.runtime.graph.batch
    call = prepared.call
    if (
        type(data) is not RestoredBatchData
        or type(prepared.runtime.source.policy) is not ContinuationAdmissionPolicyV4
        or data.batch.current_call is not call
        or call.audit is not prepared.original_audit
        or call.audit != prepared.original_audit_fields
        or call.arguments is None
        or not 0 <= call.index < len(data.calls)
        or call.tool_call is not data.calls[call.index]
        or prepared.call_model.completion.tool_batch.calls != data.calls
        or prepared.context.recorder is not prepared.runtime.live.recorder
        or prepared.context.message_producers is not prepared.runtime.materialization
    ):
        raise AuditIntegrityError("value computation changed its actual prepared owners")
    from dataclasses import replace

    comparison = RestoredPreparedBatchData(
        batch=replace(data.batch, current_call=replace(call, audit=None)),
        accumulator=data.accumulator,
        calls=data.calls,
        audit=prepared.portable_audit,
        dialect=prepared.context.tool_contract_dialect,
    )
    if encode_restored_prepared_batch_data(comparison, env_ref_names=prepared.runtime.env_ref_names) != prepared.prepared_batch_json:
        raise AuditIntegrityError("value computation changed its original prepared graph")
    return data


async def _compute(prepared: PreparedValueBatchCall) -> ValueBatchComputation:
    data = _require_original_call(prepared)
    call, ctx = prepared.call, prepared.context
    arguments = call.arguments
    if arguments is None:
        raise AuditIntegrityError("value computation lost its admitted arguments")
    declaration = continuation_declaration(call.tool_name)
    if declaration is None:
        raise ValueBatchPreparationRefused("value batch has no declared producer")
    if call.tool_name in VALUE_BATCH_READ_TOOLS:
        if declaration.kind is not ToolKind.DISCOVERY or declaration.resolve_effects(arguments).domains:
            raise AuditIntegrityError("value read declaration changed its original effects")
    elif call.tool_name in VALUE_BATCH_GRAPH_TOOLS:
        if declaration.kind is not ToolKind.MUTATION or declaration.resolve_effects(arguments) != GRAPH_EFFECTS:
            raise ValueBatchPreparationRefused("value batch requires an unsupported authored effect")
        if ctx.review_required or ctx.mutation_ceiling is None:
            raise ValueBatchPreparationRefused("value graph preparation requires original automatic mutation authority")
    else:
        raise ValueBatchPreparationRefused("value batch requires a separate bounded producer")
    if call.tool_name == "set_pipeline":
        require_no_credential_material_for_tool(
            "set_pipeline", arguments, surface="composer_tool_arguments", credential_state=call.pre_state
        )
        prior = data.batch.last_validation or ctx.policy_catalog.validate_composition_state(call.pre_state).validation
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
            current_validation=prior,
            max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
            user_message_id=ctx.user_message_id,
            user_message_content=ctx.user_message_content,
            composer_model_identifier=ctx.provenance.model_identifier,
            composer_model_version=data.batch.provider_model_version,
            composer_provider=ctx.provenance.provider or "unknown",
            composer_skill_hash=ctx.provenance.skill_hash,
            tool_arguments_hash=prepared.original_audit.binding_arguments_hash,
            mutation_ceiling=ctx.mutation_ceiling,
        )
        finalized = await _finalize_complete_set_pipeline_candidate(
            arguments, call.pre_state, candidate_context, plugin_snapshot=ctx.plugin_snapshot, policy_catalog=ctx.policy_catalog
        )
        candidate = finalized.candidate
        if type(candidate) is not SetPipelineCandidate:
            raise AuditIntegrityError("value pipeline lost its original candidate")
        if finalized.changed or finalized.arguments != arguments or candidate.prepared_inline_blob is not None:
            raise ValueBatchPreparationRefused("value pipeline requires a separate rebinding or blob tail")
        result = candidate.result
    else:
        result = await run_sync_in_worker(
            execute_tool,
            call.tool_name,
            arguments,
            call.pre_state,
            ctx.policy_catalog,
            plugin_snapshot=ctx.plugin_snapshot,
            data_dir=ctx.custody.data_dir,
            session_engine=ctx.custody.session_engine,
            session_id=ctx.session_id,
            session_operation_context=ctx.session_operation_context,
            session_operation_authority=ctx.session_operation_authority,
            secret_service=ctx.custody.secret_service,
            secret_wiring_policy=ctx.custody.secret_wiring_policy,
            user_id=ctx.user_id,
            prior_validation=data.batch.last_validation,
            max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
            user_message_id=ctx.user_message_id,
            user_message_content=ctx.user_message_content,
            composer_model_identifier=ctx.provenance.model_identifier,
            composer_model_version=data.batch.provider_model_version,
            composer_provider=ctx.provenance.provider or "unknown",
            composer_skill_hash=ctx.provenance.skill_hash,
            tool_arguments_hash=prepared.original_audit.binding_arguments_hash,
            mutation_ceiling=ctx.mutation_ceiling,
            validate_arguments=True,
            require_data_dir_for_paths=True,
            raise_schema_argument_errors=True,
        )
        if (
            declaration.kind is ToolKind.MUTATION
            and result.success
            and result.updated_state.version > call.pre_version
            and result.validation.is_valid
        ):
            result = await _finalize_completed_incremental_mutation(
                result, call.pre_state, plugin_snapshot=ctx.plugin_snapshot, policy_catalog=ctx.policy_catalog
            )
    if type(result) is not ToolResult:
        raise AuditIntegrityError("value producer replaced its nominal original result")
    if result.success and result.updated_state.sources and result.updated_state.outputs and interpretation_sites(result.updated_state):
        raise ValueBatchPreparationRefused("value result requires a separate interpretation/preflight tail")
    return ValueBatchComputation(result)


async def observe_value_batch_computation(prepared: PreparedValueBatchCall) -> ObservedValueBatchComputation:
    _require_original_call(prepared)
    call, context = prepared.call, prepared.context
    declaration = continuation_declaration(call.tool_name)
    if declaration is None or call.arguments is None:
        raise ValueBatchPreparationRefused("value computation has no original declared arguments")
    cached = context.discovery_cache.get(make_cache_key(call.tool_name, call.arguments)) if declaration.cacheable else None
    value = None
    admitted = None
    error = None
    if cached is None:
        try:
            value = await _compute(prepared)
        except ToolArgumentError as original:
            error = original
        if value is not None and declaration.kind is ToolKind.DISCOVERY and response_contract_for(call.tool_name) is not None:
            admitted = admit_discovery_result(call.tool_name, value.result)
    marker = ObservedValueBatchComputation()
    _OBSERVATIONS[marker] = _OriginalComputation(
        prepared, value, admitted, error, cached, None if value is None else _result_json(prepared, value.result)
    )
    return marker


def original_value_batch_computation(marker: ObservedValueBatchComputation) -> tuple[PreparedValueBatchCall, ValueBatchComputation | None]:
    if type(marker) is not ObservedValueBatchComputation or marker not in _OBSERVATIONS:
        raise AuditIntegrityError("value computation lacks its original producer observation")
    source = _OBSERVATIONS[marker]
    if source.value is not None and source.result_json != _result_json(source.prepared, source.value.result):
        raise AuditIntegrityError("value computation changed its original complete result")
    return source.prepared, source.value


def consume_value_batch_computation(
    marker: ObservedValueBatchComputation,
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> ToolResult | AdmittedDiscoveryResult:
    prepared, value = original_value_batch_computation(marker)
    data = _require_original_call(prepared)
    source = _OBSERVATIONS[marker]
    if (
        source.consumed
        or prepared.call_model is not call_model
        or prepared.context is not ctx
        or prepared.call is not call
        or data.batch is not batch
        or data.accumulator is not acc
        or source.cached is not None
    ):
        raise AuditIntegrityError("value dispatch changed or repeated its original computation")
    source.consumed = True
    if source.error is not None:
        raise source.error
    if value is None:
        raise AuditIntegrityError("value dispatch lost its original result")
    return source.admitted if source.admitted is not None else value.result


def require_value_batch_execution(
    marker: ObservedValueBatchComputation,
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> None:
    """Bind the alternate publication path before the ordinary worker starts."""
    prepared, _value = original_value_batch_computation(marker)
    data = _require_original_call(prepared)
    source = _OBSERVATIONS[marker]
    if (
        source.consumed
        or prepared.call_model is not call_model
        or prepared.context is not ctx
        or prepared.call is not call
        or data.batch is not batch
        or data.accumulator is not acc
    ):
        raise AuditIntegrityError("value worker changed its actual original prepared source")
    if source.cached is not None and (
        call.arguments is None or ctx.discovery_cache.get(make_cache_key(call.tool_name, call.arguments)) is not source.cached
    ):
        raise AuditIntegrityError("value worker changed its actual cache observation")


def original_value_batch_feedback_source(
    marker: ObservedValueBatchComputation,
) -> tuple[PreparedValueBatchCall, ToolArgumentError | None, CachedDiscoveryPayload | None]:
    prepared, _value = original_value_batch_computation(marker)
    source = _OBSERVATIONS[marker]
    return prepared, source.error, source.cached


def original_value_batch_result_source(
    marker: ObservedValueBatchComputation,
) -> tuple[
    PreparedValueBatchCall,
    ValueBatchComputation | None,
    AdmittedDiscoveryResult | None,
    ToolArgumentError | None,
    CachedDiscoveryPayload | None,
]:
    prepared, value = original_value_batch_computation(marker)
    source = _OBSERVATIONS[marker]
    return prepared, value, source.admitted, source.error, source.cached


def record_value_batch_cached_result(
    marker: ObservedValueBatchComputation,
    *,
    result: ToolResult,
    admitted: AdmittedDiscoveryResult | None,
) -> None:
    """Observe the actual ordinary cache branch, without making a replacement."""
    prepared, value = original_value_batch_computation(marker)
    source = _OBSERVATIONS[marker]
    if source.consumed or source.cached is None or value is not None or type(result) is not ToolResult:
        raise AuditIntegrityError("value cache result replaced its original single cache branch")
    if (
        prepared.call.arguments is None
        or prepared.context.discovery_cache.get(make_cache_key(prepared.call.tool_name, prepared.call.arguments)) is not source.cached
    ):
        raise AuditIntegrityError("value cache result changed its original cached producer")
    if admitted is not None and admitted.result is not result:
        raise AuditIntegrityError("value cache result changed its original admitted wrapper")
    source.value = ValueBatchComputation(result)
    source.admitted = admitted
    source.result_json = _result_json(prepared, result)
    source.consumed = True
