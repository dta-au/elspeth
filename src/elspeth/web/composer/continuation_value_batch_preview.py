"""Detached V4 preview DATA for the original resource-free empty producer.

This is not general preview execution. Any graph component or interpretation
handoff needs a separate complete producer bound before work.
The batch owner must still serialize/admit the entire original result,
feedback and P4 tail before SQL issue/adoption; these values grant no authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_preview_observation import (
    OriginalPreviewObservation,
    observe_original_preview_result,
    preview_source_data,
    require_original_preview_observation,
)
from elspeth.web.composer.continuation_value_batch_preparation import (
    PreparedValueBatchCall,
    ValueBatchComputation,
    ValueBatchPreparationRefused,
)
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.execution.runtime_preflight import RuntimePreflightKey
from elspeth.web.execution.schemas import ValidationResult


@dataclass(frozen=True, slots=True)
class PreviewValueSourcePlan:
    """An exact empty-state source, not dispatch or whole-tail reservation."""

    prepared: PreparedValueBatchCall
    data: RestoredBatchData
    arguments: Mapping[str, JsonValue]
    key: RuntimePreflightKey
    cached: ValidationResult | None
    cached_json: str | None

    def __post_init__(self) -> None:
        if type(self.arguments) is not dict or self.arguments:
            raise TypeError("preview source requires its validated empty argument snapshot")
        freeze_fields(self, "arguments")


def plan_preview_value_source(prepared: PreparedValueBatchCall) -> PreviewValueSourcePlan:
    """Refuse unsupported branches before preflight/dispatcher work or reads."""
    if type(prepared) is not PreparedValueBatchCall:
        raise TypeError("preview preparation requires its original value-batch call")
    ctx, call, runtime = prepared.context, prepared.call, prepared.runtime
    data = runtime.graph.batch
    if type(data) is not RestoredBatchData:
        raise AuditIntegrityError("preview preparation lost its original detached batch")
    arguments = call.arguments
    if (
        data.batch.current_call is not call
        or call.audit is not prepared.original_audit
        or call.audit != prepared.original_audit_fields
        or call.pre_state is not data.batch.state
        or ctx.runtime_preflight_cache is not runtime.live.runtime_preflight_cache
        or ctx.recorder is not runtime.live.recorder
        or ctx.policy_catalog.snapshot is not ctx.plugin_snapshot
    ):
        raise AuditIntegrityError("preview preparation changed its original detached source")
    if (
        type(runtime.source.policy) is not ContinuationAdmissionPolicyV4
        or call.tool_name != "preview_pipeline"
        or call.tool_call.function.name != "preview_pipeline"
        or call.phase != "prepared"
        or type(arguments) is not dict
        or arguments
        or type(call.pre_state) is not CompositionState
        or call.pre_state.sources
        or call.pre_state.nodes
        or call.pre_state.outputs
        or type(ctx.preflight) is not ComposerPreflight
        or type(ctx.policy_catalog) is not PolicyCatalogView
        or type(ctx.runtime_preflight_cache) is not dict
        or ctx.cancellation_requested.is_set()
    ):
        raise ValueBatchPreparationRefused("preview requires an uncancelled V4 empty graph and complete original arguments")
    key = ctx.preflight.key(call.pre_state, session_scope=ctx.session_scope, plugin_snapshot=ctx.plugin_snapshot)
    cache = ctx.runtime_preflight_cache
    cached = cache[key] if key in cache else None
    if cached is not None and (type(cached) is not ValidationResult or is_pending_interpretation_handoff(cached)):
        raise ValueBatchPreparationRefused("preview cannot use a foreign, failed or interpretation-pending verdict")
    return PreviewValueSourcePlan(
        prepared,
        data,
        cast(dict[str, JsonValue], arguments),
        key,
        cached,
        None if cached is None else canonical_json(cached.model_dump(mode="json")),
    )


async def observe_empty_preview_value(prepared: PreparedValueBatchCall) -> OriginalPreviewObservation:
    """Produce the original pure value; never issue, publish or discard a result.

    The real runtime validator returns its complete fixed empty-pipeline
    verdict before plugin/secret/path/resource work. Its real coordinator and
    detached cache retain their normal miss/hit/cancellation behavior. Empty
    authoring avoids plugin probes; source proof returns not-applicable before
    a resolver. Core owns audit and full-tail admission before SQL issue.
    """
    plan = plan_preview_value_source(prepared)
    source = preview_source_data(prepared)
    ctx, call = prepared.context, prepared.call
    arguments = call.arguments
    if type(arguments) is not dict or arguments != plan.arguments:
        raise AuditIntegrityError("preview changed its original parsed arguments")
    cache_before = dict(ctx.runtime_preflight_cache)
    strict = await ctx.preflight.cached_runtime_preflight(
        call.pre_state,
        user_id=ctx.user_id,
        session_id=ctx.session_id,
        cache=ctx.runtime_preflight_cache,
        initial_version=ctx.initial_version,
        session_scope=ctx.session_scope,
        llm_calls=ctx.recorder.llm_calls,
        plugin_snapshot=ctx.plugin_snapshot,
        session_operation_context=ctx.session_operation_context,
    )
    if (
        type(strict) is not ValidationResult
        or is_pending_interpretation_handoff(strict)
        or ctx.runtime_preflight_cache != {**cache_before, plan.key: strict}
        or (plan.cached is not None and strict is not plan.cached)
        or (plan.cached_json is not None and canonical_json(strict.model_dump(mode="json")) != plan.cached_json)
    ):
        raise AuditIntegrityError("preview changed its original empty verdict or detached cache")
    strict_json = canonical_json(strict.model_dump(mode="json"))

    def original_preflight(_state: CompositionState) -> ValidationResult:
        if _state is not call.pre_state:
            raise AuditIntegrityError("preview callback changed its original state")
        return strict

    result = await run_sync_in_worker(
        execute_tool,
        "preview_pipeline",
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
        prior_validation=plan.data.batch.last_validation,
        runtime_preflight=original_preflight,
        structural_preflight=None,
        max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
        user_message_id=ctx.user_message_id,
        user_message_content=ctx.user_message_content,
        composer_model_identifier=ctx.provenance.model_identifier,
        composer_model_version=plan.data.batch.provider_model_version,
        composer_provider=ctx.provenance.provider or "unknown",
        composer_skill_hash=ctx.provenance.skill_hash,
        tool_arguments_hash=prepared.original_audit.binding_arguments_hash,
        mutation_ceiling=ctx.mutation_ceiling,
        validate_arguments=True,
        require_data_dir_for_paths=True,
        raise_schema_argument_errors=True,
    )
    if (
        type(result) is not ToolResult
        or result.updated_state is not call.pre_state
        or result.runtime_preflight is not strict
        or ctx.runtime_preflight_cache != {**cache_before, plan.key: strict}
        or canonical_json(strict.model_dump(mode="json")) != strict_json
    ):
        raise AuditIntegrityError("preview changed its original pure result or empty source")
    return observe_original_preview_result(prepared, source=source, result=result, strict=strict)


async def compute_preview_value(prepared: PreparedValueBatchCall) -> ValueBatchComputation:
    observed = await observe_empty_preview_value(prepared)
    require_original_preview_observation(observed, prepared)
    return ValueBatchComputation(observed.result)
