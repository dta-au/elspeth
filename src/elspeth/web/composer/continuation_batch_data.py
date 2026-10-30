"""Coupled batch/accumulator DATA at quiescent and prepared live-B2 cuts.

This does not restore execution custody, a dispatch timer, callback, committed
P4 witness, or required-work owner. References identify objects, never equal
JSON. Constructor-normalized alias topologies are refused, not flattened.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import TypeAdapter
from pydantic import ValidationError as ModelValidationError

from elspeth.contracts.composer_audit import (
    ComposerToolInvocation,
    ComposerToolStatus,
    PrivateFailureFacts,
    PrivateToolAuthoring,
    ToolArgumentErrorCategory,
)
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.enums import AuditCharacteristic
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import FrozenJsonArray
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlRole, PluginCapability, WebConfigAuthority
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.catalog.schemas import ConfigFieldSummary, PluginSchemaInfo, PluginSecretRequirement, PluginSummary
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome
from elspeth.web.composer._schema_response_grammar import JSONSchemaSnapshot, KnobSchemaSnapshot
from elspeth.web.composer.audit import DispatchAudit
from elspeth.web.composer.authority_hashing import composer_authority_canonical_json
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES, bounded_json_loads
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, owned_json, verify_private_chunk
from elspeth.web.composer.continuation_dispatch_audit import (
    PortableDispatchAudit,
    capture_dispatch_audit,
    decode_dispatch_audit,
    encode_dispatch_audit,
    re_admit_dispatch_audit,
)
from elspeth.web.composer.continuation_result_credentials import require_retained_argument_credentials, require_retained_result_credentials
from elspeth.web.composer.continuation_tool_batch import (
    RetainedAdvisorRefusalSource,
    RetainedBatchFailure,
    _decode_validation,
    _read_json_value,
    _state_from_tool_json,
    _validation_json,
    encode_tool_result_value,
)
from elspeth.web.composer.continuation_tool_receipts import (
    decode_retained_tool_receipt,
    require_receipt_result,
)
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result
from elspeth.web.composer.manual_advisor_allowance import ManualAdvisorAllowance
from elspeth.web.composer.pipeline_custody import inline_custody_audit_projection
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState, ContinuationCallState, _CallWireFacts
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools._generation_schema_response import (
    PLUGIN_SCHEMA_RESPONSE_CONTRACT,
    AdmittedPluginSchemaResponse,
    PluginSchemaSnapshot,
    SchemaSecretRequirement,
)
from elspeth.web.composer.tools._registry import response_contract_for
from elspeth.web.composer.tools.wire_projection import _WIRE_TOOL_DEFS, decode_wire_arguments
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.execution.schemas import (
    SemanticEdgeContractResponse,
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
    ValidationWarning,
)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_application_graph import ApplicationGraph

_MAX_NODES = 100_000
_MAX_DEPTH = 128


@dataclass(frozen=True, slots=True)
class RestoredBatchData:
    """Historical data only; callers still owe all fresh execution owners."""

    batch: ContinuationBatchState
    accumulator: BatchAccumulator
    calls: tuple[_AdmittedToolCall, ...]


@dataclass(frozen=True, slots=True)
class RestoredPreparedBatchData:
    """Complete prepared DATA; actual call.audit stays None until readmission.

    The historical audit is retained here, never discarded or put into a field
    whose runtime contract is DispatchAudit. No current execution authority is
    created. The caller still owes the fresh F7 context/checkpoint admission.
    """

    batch: ContinuationBatchState
    accumulator: BatchAccumulator
    calls: tuple[_AdmittedToolCall, ...]
    audit: PortableDispatchAudit
    dialect: ToolContractDialect


@dataclass(frozen=True, slots=True)
class RestoredMemoryCompletionBatchData:
    """Completed memory result DATA before application, never a current grant.

    The completed receipt preserves original invocation timing. The portable
    audit remains historical context; application must not finish it again.
    """

    batch: ContinuationBatchState
    accumulator: BatchAccumulator
    calls: tuple[_AdmittedToolCall, ...]
    audit: PortableDispatchAudit
    dialect: ToolContractDialect


def _fail(message: str) -> NoReturn:
    raise AuditIntegrityError(message)


def _safe(value: Any, env_ref_names: frozenset[str]) -> None:
    require_no_credential_material(owned_json(value), surface="retained batch data", env_ref_names=env_ref_names)


def _argument_text(tool_name: str, text: str | None, env_ref_names: frozenset[str]) -> None:
    if text is None:
        return
    from elspeth.contracts.json_parser import parse_json_strict

    parsed, error = parse_json_strict(text)
    if error is None:
        require_retained_argument_credentials(tool_name, owned_json(parsed), env_ref_names=env_ref_names)
    else:
        _safe(text, env_ref_names)


def _joint(
    data: RestoredBatchData,
    env_ref_names: frozenset[str],
    prepared: tuple[PortableDispatchAudit, ToolContractDialect] | None = None,
    *,
    memory_completion: bool = False,
) -> dict[int, str]:
    if (
        type(data) is not RestoredBatchData
        or type(data.batch) is not ContinuationBatchState
        or type(data.accumulator) is not BatchAccumulator
    ):
        raise TypeError("batch retention requires the actual coupled owners")
    batch, acc, calls = data.batch, data.accumulator, data.calls
    # This live-only owner has no retained wire representation. Every capture
    # path (quiescent, prepared and completed) shares this check before encoding:
    # cold reconstruction may restore only the settled spent integer, never
    # silently drop an active dispatch/refund obligation.
    if batch.manual_advisor_allowance is not None:
        if type(batch.manual_advisor_allowance) is not ManualAdvisorAllowance:
            _fail("batch manual Advisor allowance lost its nominal live owner")
        batch.manual_advisor_allowance.require_settled(used=batch.advisor_calls_used)
    if memory_completion and prepared is None:
        _fail("memory completion lost its retained audit/dialect")
    if type(calls) is not tuple or any(
        type(call) is not _AdmittedToolCall or type(call.function) is not _AdmittedToolFunction for call in calls
    ):
        raise TypeError("batch retention requires the actual admitted call tuple")
    ids = [call.id for call in calls]
    if any(type(value) is not str or not value for value in ids) or len(set(ids)) != len(ids):
        _fail("retained batch has duplicate or invalid admitted call identities")
    # Quiescent APIs keep their original refusal. The separate prepared DATA
    # API retains the audit outside the actual call until explicit readmission.
    if prepared is None:
        if batch.current_call is not None or batch.phase not in {"call_ready", "dispatch_ready"}:
            _fail("batch cut requires current call/timing owner admission")
    elif type(batch.current_call) is not ContinuationCallState or batch.phase != "call_active":
        _fail("prepared batch lost its actual active call owner")
    if batch.plugin_crash is not None or batch.plugin_crash_cause is not None or batch.advisor_failure is not None:
        _fail("batch cut contains a live exception owner")
    if batch.p4 is not None or batch.p4_receipt_json is not None or batch.p4_input_json is not None:
        _fail("batch cut contains a P4 input or committed witness owned by a separate codec")
    if type(batch.next_tool_index) is not int or not 0 <= batch.next_tool_index <= len(calls):
        _fail("retained batch cursor is outside its admitted calls")
    if len(batch.tool_outcomes) != batch.next_tool_index:
        _fail("quiescent batch cursor differs from its completed prefix")
    if batch.phase == "call_ready" and batch.stop_issued_batch:
        _fail("stopped batch must retain its dispatch-ready cut")
    if batch.phase == "dispatch_ready" and batch.next_tool_index != len(calls) and not batch.stop_issued_batch:
        _fail("partial dispatch-ready prefix lost its stop fact")
    if any(type(value) is not CompositionState for value in (batch.state, batch.entry_state, acc.state)):
        _fail("batch state roots are not nominal states")
    for validation_summary in (batch.last_validation, acc.last_validation):
        if validation_summary is not None and type(validation_summary) is not ValidationSummary:
            _fail("batch validation root is not its nominal summary")
    for runtime_preflight in (batch.last_runtime_preflight, acc.last_runtime_preflight):
        if runtime_preflight is not None and type(runtime_preflight) is not ValidationResult:
            _fail("batch preflight root is not its nominal result")
    for retained_count in (batch.advisor_calls_used, acc.advisor_calls_used, batch.proposals_this_turn):
        if type(retained_count) is not int or retained_count < 0:
            _fail("batch count is not an exact nonnegative integer")
    for batch_flag in (
        batch.early_reviewed,
        batch.mixed_abandonment_batch,
        batch.turn_has_mutation,
        batch.turn_has_discovery,
        batch.all_cache_hits,
        batch.mutation_success_observed,
        batch.stop_issued_batch,
        batch.p4_verification_failed,
    ):
        if type(batch_flag) is not bool:
            _fail("batch flag is not an exact boolean")
    if batch.assistant_message_index is not None and (type(batch.assistant_message_index) is not int or batch.assistant_message_index < 0):
        _fail("batch assistant index is not an exact nonnegative integer")
    if type(batch.provider_model_version) is not str or (batch.pre_state_id is not None and type(batch.pre_state_id) is not str):
        _fail("batch retained identity is not text")
    if batch.advisor_compose_timeout not in (None, "pre_call", "in_flight"):
        _fail("batch timeout fact is outside its vocabulary")
    if batch.retained_failure is not None and type(batch.retained_failure) is not RetainedBatchFailure:
        _fail("batch historical failure is not its closed descriptor")
    if acc.authority_revocation is not None and type(acc.authority_revocation) is not ComposerMutationRevocation:
        _fail("batch revocation is not the actual historical value")
    if (
        type(batch.tool_outcomes) is not list
        or type(batch.decoded_args_by_call_id) is not dict
        or type(batch.private_authorings_by_call) is not dict
    ):
        _fail("batch collections changed their actual mutable owners")
    if type(batch.promises_by_call) is not dict or type(batch.refused_resources) is not set:
        _fail("batch resource collections changed their actual owners")
    for resources in (batch.refused_resources, *batch.promises_by_call.values()):
        if type(resources) not in (set, frozenset) or any(
            type(item) is not tuple or len(item) != 2 or any(type(part) is not str for part in item) for item in resources
        ):
            _fail("batch resources are not exact resource pairs")
        _safe(list(resources), env_ref_names)
    if any(type(resources) is not frozenset for resources in batch.promises_by_call.values()):
        _fail("batch promises lost their frozen resource set")
    by_id = {call.id: call for call in calls}
    result_tools: dict[int, str] = {}
    for call in calls:
        if type(call.function.name) is not str or not call.function.name or type(call.function.arguments) is not str:
            _fail("admitted call lost its exact function fields")
        _safe([call.id, call.function.name], env_ref_names)
        _argument_text(call.function.name, call.function.arguments, env_ref_names)
    for index, outcome in enumerate(batch.tool_outcomes):
        if type(outcome) is not _ToolOutcome or outcome.call is not calls[index]:
            _fail("batch outcome lost the actual admitted call identity/order")
        if (
            type(outcome.pre_version) is not int
            or type(outcome.post_version) is not int
            or min(outcome.pre_version, outcome.post_version) < 1
        ):
            _fail("batch outcome lost exact positive versions")
        for flag in (outcome.strict_sent, outcome.wire_conformant):
            if flag is not None and type(flag) is not bool:
                _fail("batch wire fact is not an exact boolean or absent")
        if outcome.error_category is not None and (
            type(outcome.error_category) is not ToolArgumentErrorCategory or outcome.error_class is None
        ):
            _fail("batch outcome lost its nominal argument-error category")
        if any(value is not None and type(value) is not str for value in (outcome.error_class, outcome.error_message)):
            _fail("batch outcome error facts are not exact strings or absent")
        _safe([outcome.error_class, outcome.error_message], env_ref_names)
        response = outcome.response
        if type(response) is ToolResult:
            if id(response) in result_tools and result_tools[id(response)] != outcome.call.function.name:
                _fail("one retained ToolResult cannot have two selected tool contracts")
            result_tools[id(response)] = outcome.call.function.name
            # Select the actual nominal response owner before projecting JSON;
            # ToolResult.to_dict() may still contain PluginSchemaInfo/models.
            envelope = (
                admit_discovery_result(outcome.call.function.name, response).to_dict()
                if response_contract_for(outcome.call.function.name) is not None
                else response.to_dict()
            )
            require_retained_result_credentials(
                outcome.call.function.name, _read_json_value(canonical_json(envelope)), env_ref_names=env_ref_names
            )
        elif response is not None:
            if type(response) is not MappingProxyType or outcome.call.function.name != "request_advisor_hint":
                _fail("batch mapping response is outside its actual Advisor envelope owner")
            _safe(response, env_ref_names)
    for key, arguments in batch.decoded_args_by_call_id.items():
        if key not in by_id or type(arguments) is not dict:
            _fail("batch decoded arguments have no admitted call")
        require_retained_argument_credentials(by_id[key].function.name, owned_json(arguments), env_ref_names=env_ref_names)
    authoring_count = batch.next_tool_index + (1 if prepared is not None else 0)
    if set(batch.private_authorings_by_call) != set(ids[:authoring_count]):
        _fail("batch private authoring cohort differs from its completed prefix")
    for key, authoring in batch.private_authorings_by_call.items():
        if (
            key not in by_id
            or type(authoring) is not PrivateToolAuthoring
            or authoring.tool_call_id != key
            or authoring.tool_name != by_id[key].function.name
        ):
            _fail("batch private authoring lost its selected call binding")
        if authoring.raw_arguments != by_id[key].function.arguments:
            _fail("batch private authoring differs from admitted raw arguments")
        for text in (authoring.raw_arguments, authoring.arguments_canonical, authoring.authority_arguments_canonical):
            _argument_text(authoring.tool_name, text, env_ref_names)
    if any(key not in by_id for key in batch.promises_by_call):
        _fail("batch resource promise has no admitted call")
    _safe([batch.provider_model_version, batch.pre_state_id, list(batch.refused_resources), list(batch.promises_by_call)], env_ref_names)
    if prepared is not None:
        _check_prepared(data, prepared[0], prepared[1], env_ref_names, memory_completion=memory_completion)
        if memory_completion:
            _check_memory_completion(data, prepared[0], env_ref_names, result_tools)
    from elspeth.web.composer.continuation_tool_failure import require_retained_failure_links

    require_retained_failure_links(data)
    return result_tools


def _members(value: Any, selected_tool: str | None = None) -> tuple[str, dict[str, Any]]:
    """Explicit actual owned fields; no dynamic field discovery or default fill."""
    if type(value) is AdmittedDiscoveryResult:
        if selected_tool is None:
            _fail("admitted discovery graph lost its selected tool contract")
        return "AdmittedDiscoveryResult", {"result": value.result, "response": value.response, "tool_name": selected_tool}
    if type(value) is AdmittedPluginSchemaResponse:
        return "AdmittedPluginSchemaResponse", {"snapshot": value.snapshot}
    if type(value) is PluginSchemaSnapshot:
        return "PluginSchemaSnapshot", {
            "name": value.name,
            "plugin_type": value.plugin_type,
            "description": value.description,
            "json_schema": value.json_schema,
            "knob_schema": value.knob_schema,
            "composer_hints": value.composer_hints,
            "secret_requirements": value.secret_requirements,
            "web_config_authority": value.web_config_authority,
            "policy_capabilities": value.policy_capabilities,
            "aggregation_output_modes": value.aggregation_output_modes,
        }
    if type(value) is SchemaSecretRequirement:
        return "SchemaSecretRequirement", {"field": value.field, "candidates": value.candidates}
    if type(value) is JSONSchemaSnapshot:
        return "JSONSchemaSnapshot", {"keywords": value.keywords}
    if type(value) is KnobSchemaSnapshot:
        return "KnobSchemaSnapshot", {"fields": value.fields}
    if type(value) is ContinuationCallState:
        return "ContinuationCallState", {
            "index": value.index,
            "tool_call": value.tool_call,
            "pre_state": value.pre_state,
            "pre_version": value.pre_version,
            "phase": value.phase,
            "tool_name": value.tool_name,
            "private_raw_arguments": value.private_raw_arguments,
            "private_arguments_canonical": value.private_arguments_canonical,
            "private_authority_canonical": value.private_authority_canonical,
            "unknown_audit_arguments": value.unknown_audit_arguments,
            "wire": value.wire,
            "arguments": value.arguments,
            "audit_arguments": value.audit_arguments,
            "audit": value.audit,
            "effect_key": value.effect_key,
            "owner_result_kind": value.owner_result_kind,
            "owner_result_json": value.owner_result_json,
            "memory_result": value.memory_result,
            "original_creator_id": value.original_creator_id,
            "original_creator_epoch": value.original_creator_epoch,
            "completion_json": value.completion_json,
            "owner_tool_receipt_json": value.owner_tool_receipt_json,
            "advisor_invocation_json": value.advisor_invocation_json,
        }
    if type(value) is _CallWireFacts:
        return "_CallWireFacts", {"strict_sent": value.strict_sent, "wire_conformant": value.wire_conformant}
    if type(value) is BatchAccumulator:
        return "BatchAccumulator", {
            "state": value.state,
            "last_validation": value.last_validation,
            "last_runtime_preflight": value.last_runtime_preflight,
            "advisor_calls_used": value.advisor_calls_used,
            "authority_revocation": value.authority_revocation,
        }
    if type(value) is ContinuationBatchState:
        return "ContinuationBatchState", {
            "state": value.state,
            "last_validation": value.last_validation,
            "last_runtime_preflight": value.last_runtime_preflight,
            "advisor_calls_used": value.advisor_calls_used,
            "entry_state": value.entry_state,
            "early_reviewed": value.early_reviewed,
            "phase": value.phase,
            "next_tool_index": value.next_tool_index,
            "assistant_message_index": value.assistant_message_index,
            "mixed_abandonment_batch": value.mixed_abandonment_batch,
            "provider_model_version": value.provider_model_version,
            "turn_has_mutation": value.turn_has_mutation,
            "turn_has_discovery": value.turn_has_discovery,
            "all_cache_hits": value.all_cache_hits,
            "tool_outcomes": value.tool_outcomes,
            "plugin_crash": value.plugin_crash,
            "plugin_crash_cause": value.plugin_crash_cause,
            "advisor_failure": value.advisor_failure,
            "advisor_compose_timeout": value.advisor_compose_timeout,
            "pre_state_id": value.pre_state_id,
            "decoded_args_by_call_id": value.decoded_args_by_call_id,
            "private_authorings_by_call": value.private_authorings_by_call,
            "proposals_this_turn": value.proposals_this_turn,
            "mutation_success_observed": value.mutation_success_observed,
            "refused_resources": value.refused_resources,
            "promises_by_call": value.promises_by_call,
            "current_call": value.current_call,
            "stop_issued_batch": value.stop_issued_batch,
            "p4": value.p4,
            "retained_failure": value.retained_failure,
            "p4_receipt_json": value.p4_receipt_json,
            "p4_input_json": value.p4_input_json,
            "p4_verification_failed": value.p4_verification_failed,
        }
    if type(value) is _AdmittedToolCall:
        return "_AdmittedToolCall", {
            "id": value.id,
            "function": value.function,
        }
    if type(value) is _AdmittedToolFunction:
        return "_AdmittedToolFunction", {
            "name": value.name,
            "arguments": value.arguments,
        }
    if type(value) is _ToolOutcome:
        return "_ToolOutcome", {
            "call": value.call,
            "response": value.response,
            "error_class": value.error_class,
            "error_category": value.error_category,
            "error_message": value.error_message,
            "pre_version": value.pre_version,
            "post_version": value.post_version,
            "strict_sent": value.strict_sent,
            "wire_conformant": value.wire_conformant,
        }
    if type(value) is PrivateToolAuthoring:
        return "PrivateToolAuthoring", {
            "tool_call_id": value.tool_call_id,
            "tool_name": value.tool_name,
            "arguments_canonical": value.arguments_canonical,
            "authority_arguments_canonical": value.authority_arguments_canonical,
            "raw_arguments": value.raw_arguments,
        }
    if type(value) is ComposerMutationCeiling:
        return "ComposerMutationCeiling", {
            "session_id": value.session_id,
            "request_id": value.request_id,
            "initial_trust_mode": value.initial_trust_mode,
        }
    if type(value) is ComposerMutationRevocation:
        return "ComposerMutationRevocation", {
            "ceiling": value.ceiling,
            "current_trust_mode": value.current_trust_mode,
        }
    if type(value) is ToolResult:
        return "ToolResult", {
            "success": value.success,
            "updated_state": value.updated_state,
            "validation": value.validation,
            "affected_nodes": value.affected_nodes,
            "data": value.data,
            "prior_validation": value.prior_validation,
            "runtime_preflight": value.runtime_preflight,
            "post_call_hints": value.post_call_hints,
            "plugin_schemas": value.plugin_schemas,
            "validation_guidance": value.validation_guidance,
            "applied_component": value.applied_component,
            "_validation_snapshot_hash": value._validation_snapshot_hash,
            "_state_validation_withheld": value._state_validation_withheld,
        }
    if type(value) is ValidationResult:
        return "ValidationResult", {
            "is_valid": value.is_valid,
            "checks": value.checks,
            "errors": value.errors,
            "warnings": value.warnings,
            "readiness": value.readiness,
            "semantic_contracts": value.semantic_contracts,
        }
    if type(value) is ValidationCheck:
        return "ValidationCheck", {
            "name": value.name,
            "passed": value.passed,
            "detail": value.detail,
            "request_rejection": value.request_rejection,
            "affected_nodes": value.affected_nodes,
            "outcome_code": value.outcome_code,
        }
    if type(value) is ValidationError:
        return "ValidationError", {
            "component_id": value.component_id,
            "component_type": value.component_type,
            "message": value.message,
            "suggestion": value.suggestion,
            "error_code": value.error_code,
        }
    if type(value) is ValidationWarning:
        return "ValidationWarning", {
            "component_id": value.component_id,
            "component_type": value.component_type,
            "message": value.message,
            "suggestion": value.suggestion,
            "warning_code": value.warning_code,
        }
    if type(value) is SemanticEdgeContractResponse:
        return "SemanticEdgeContractResponse", {
            "from_id": value.from_id,
            "to_id": value.to_id,
            "consumer_plugin": value.consumer_plugin,
            "producer_plugin": value.producer_plugin,
            "producer_field": value.producer_field,
            "consumer_field": value.consumer_field,
            "outcome": value.outcome,
            "requirement_code": value.requirement_code,
        }
    if type(value) is ValidationReadinessBlocker:
        return "ValidationReadinessBlocker", {
            "code": value.code,
            "component_id": value.component_id,
            "component_type": value.component_type,
            "detail": value.detail,
            "suggestion": value.suggestion,
            "request_rejection": value.request_rejection,
            "note": value.note,
        }
    if type(value) is ValidationReadiness:
        return "ValidationReadiness", {
            "authoring_valid": value.authoring_valid,
            "execution_ready": value.execution_ready,
            "completion_ready": value.completion_ready,
            "blockers": value.blockers,
        }
    if type(value) is SafeRequestRejection:
        return "SafeRequestRejection", {
            "category": value.category,
            "exception_type": value.exception_type,
            "reason_code": value.reason_code,
            "reason_disposition": value.reason_disposition,
            "reason_source": value.reason_source,
            "diagnostic_reference": value.diagnostic_reference,
            "retryable": value.retryable,
        }
    if type(value) is RetainedBatchFailure:
        return "RetainedBatchFailure", {
            "kind": value.kind,
            "error_class": value.error_class,
            "refusal_source": value.refusal_source,
        }
    if type(value) is RetainedAdvisorRefusalSource:
        return "RetainedAdvisorRefusalSource", {
            "kind": value.kind,
            "outcome": value.outcome,
            "invocation": value.invocation,
            "provider_call_id": value.provider_call_id,
            "allowance_limit": value.allowance_limit,
        }
    if type(value) is PluginSchemaInfo:
        return "PluginSchemaInfo", {
            "name": value.name,
            "plugin_type": value.plugin_type,
            "description": value.description,
            "json_schema": value.json_schema,
            "knob_schema": value.knob_schema,
            "composer_hints": value.composer_hints,
            "secret_requirements": value.secret_requirements,
            "web_config_authority": value.web_config_authority,
            "policy_capabilities": value.policy_capabilities,
            "aggregation_output_modes": value.aggregation_output_modes,
        }
    if type(value) is PluginSummary:
        return "PluginSummary", {
            "name": value.name,
            "description": value.description,
            "plugin_type": value.plugin_type,
            "config_fields": value.config_fields,
            "usage_when_to_use": value.usage_when_to_use,
            "usage_when_not_to_use": value.usage_when_not_to_use,
            "example_use": value.example_use,
            "capability_tags": value.capability_tags,
            "aggregation_output_modes": value.aggregation_output_modes,
            "web_config_authority": value.web_config_authority,
            "policy_capabilities": value.policy_capabilities,
            "audit_characteristics": value.audit_characteristics,
            "composer_hints": value.composer_hints,
            "secret_requirements": value.secret_requirements,
        }
    if type(value) is ConfigFieldSummary:
        return "ConfigFieldSummary", {
            "name": value.name,
            "type": value.type,
            "required": value.required,
            "description": value.description,
            "default": value.default,
        }
    if type(value) is PluginSecretRequirement:
        return "PluginSecretRequirement", {
            "field": value.field,
            "candidates": value.candidates,
        }
    if type(value) is CapabilityDeclaration:
        return "CapabilityDeclaration", {
            "capability": value.capability,
            "control_role": value.control_role,
            "blocks_positive_detection": value.blocks_positive_detection,
        }
    raise AuditIntegrityError("batch graph contains an unsupported live or nominal owner")


_FIELDS = {
    "AdmittedDiscoveryResult": ("result", "response", "tool_name"),
    "AdmittedPluginSchemaResponse": ("snapshot",),
    "PluginSchemaSnapshot": (
        "name",
        "plugin_type",
        "description",
        "json_schema",
        "knob_schema",
        "composer_hints",
        "secret_requirements",
        "web_config_authority",
        "policy_capabilities",
        "aggregation_output_modes",
    ),
    "SchemaSecretRequirement": ("field", "candidates"),
    "JSONSchemaSnapshot": ("keywords",),
    "KnobSchemaSnapshot": ("fields",),
    "ContinuationCallState": (
        "index",
        "tool_call",
        "pre_state",
        "pre_version",
        "phase",
        "tool_name",
        "private_raw_arguments",
        "private_arguments_canonical",
        "private_authority_canonical",
        "unknown_audit_arguments",
        "wire",
        "arguments",
        "audit_arguments",
        "audit",
        "effect_key",
        "owner_result_kind",
        "owner_result_json",
        "memory_result",
        "original_creator_id",
        "original_creator_epoch",
        "completion_json",
        "owner_tool_receipt_json",
        "advisor_invocation_json",
    ),
    "_CallWireFacts": ("strict_sent", "wire_conformant"),
    "PluginSchemaInfo": (
        "name",
        "plugin_type",
        "description",
        "json_schema",
        "knob_schema",
        "composer_hints",
        "secret_requirements",
        "web_config_authority",
        "policy_capabilities",
        "aggregation_output_modes",
    ),
    "PluginSummary": (
        "name",
        "description",
        "plugin_type",
        "config_fields",
        "usage_when_to_use",
        "usage_when_not_to_use",
        "example_use",
        "capability_tags",
        "aggregation_output_modes",
        "web_config_authority",
        "policy_capabilities",
        "audit_characteristics",
        "composer_hints",
        "secret_requirements",
    ),
    "ConfigFieldSummary": ("name", "type", "required", "description", "default"),
    "PluginSecretRequirement": ("field", "candidates"),
    "CapabilityDeclaration": ("capability", "control_role", "blocks_positive_detection"),
    "BatchAccumulator": ("state", "last_validation", "last_runtime_preflight", "advisor_calls_used", "authority_revocation"),
    "ContinuationBatchState": (
        "state",
        "last_validation",
        "last_runtime_preflight",
        "advisor_calls_used",
        "entry_state",
        "early_reviewed",
        "phase",
        "next_tool_index",
        "assistant_message_index",
        "mixed_abandonment_batch",
        "provider_model_version",
        "turn_has_mutation",
        "turn_has_discovery",
        "all_cache_hits",
        "tool_outcomes",
        "plugin_crash",
        "plugin_crash_cause",
        "advisor_failure",
        "advisor_compose_timeout",
        "pre_state_id",
        "decoded_args_by_call_id",
        "private_authorings_by_call",
        "proposals_this_turn",
        "mutation_success_observed",
        "refused_resources",
        "promises_by_call",
        "current_call",
        "stop_issued_batch",
        "p4",
        "retained_failure",
        "p4_receipt_json",
        "p4_input_json",
        "p4_verification_failed",
    ),
    "_AdmittedToolCall": ("id", "function"),
    "_AdmittedToolFunction": ("name", "arguments"),
    "_ToolOutcome": (
        "call",
        "response",
        "error_class",
        "error_category",
        "error_message",
        "pre_version",
        "post_version",
        "strict_sent",
        "wire_conformant",
    ),
    "PrivateToolAuthoring": ("tool_call_id", "tool_name", "arguments_canonical", "authority_arguments_canonical", "raw_arguments"),
    "ComposerMutationCeiling": ("session_id", "request_id", "initial_trust_mode"),
    "ComposerMutationRevocation": ("ceiling", "current_trust_mode"),
    "ToolResult": (
        "success",
        "updated_state",
        "validation",
        "affected_nodes",
        "data",
        "prior_validation",
        "runtime_preflight",
        "post_call_hints",
        "plugin_schemas",
        "validation_guidance",
        "applied_component",
        "_validation_snapshot_hash",
        "_state_validation_withheld",
    ),
    "ValidationResult": ("is_valid", "checks", "errors", "warnings", "readiness", "semantic_contracts"),
    "ValidationCheck": ("name", "passed", "detail", "request_rejection", "affected_nodes", "outcome_code"),
    "ValidationError": ("component_id", "component_type", "message", "suggestion", "error_code"),
    "ValidationWarning": ("component_id", "component_type", "message", "suggestion", "warning_code"),
    "SemanticEdgeContractResponse": (
        "from_id",
        "to_id",
        "consumer_plugin",
        "producer_plugin",
        "producer_field",
        "consumer_field",
        "outcome",
        "requirement_code",
    ),
    "ValidationReadinessBlocker": ("code", "component_id", "component_type", "detail", "suggestion", "request_rejection", "note"),
    "ValidationReadiness": ("authoring_valid", "execution_ready", "completion_ready", "blockers"),
    "SafeRequestRejection": (
        "category",
        "exception_type",
        "reason_code",
        "reason_disposition",
        "reason_source",
        "diagnostic_reference",
        "retryable",
    ),
    "RetainedBatchFailure": ("kind", "error_class", "refusal_source"),
    "RetainedAdvisorRefusalSource": ("kind", "outcome", "invocation", "provider_call_id", "allowance_limit"),
}


def _constructor_shape(value: Any) -> Any:
    """Compare values and nominal/container kinds, without conflating aliases."""
    if value is None or type(value) in (str, int, float, bool):
        return [type(value).__name__, value]
    if type(value) in (AuditCharacteristic, ControlRole, PluginCapability, WebConfigAuthority):
        return [type(value).__name__, value.value]
    if type(value) in (list, tuple, FrozenJsonArray):
        return [type(value).__name__, [_constructor_shape(item) for item in value]]
    if type(value) in (dict, MappingProxyType):
        return [type(value).__name__, [[key, _constructor_shape(item)] for key, item in value.items()]]
    kind, members = _members(value)
    return [kind, [[key, _constructor_shape(item)] for key, item in members.items()]]


def _construct(kind: str, members: dict[str, Any]) -> Any:
    if kind not in _FIELDS or tuple(members) != _FIELDS[kind]:
        _fail("batch graph lost complete ordered nominal members")
    if kind == "AdmittedDiscoveryResult":
        selected_contract = response_contract_for(members["tool_name"])
        if selected_contract is None:
            _fail("retained discovery has no actual selected response contract")
        return AdmittedDiscoveryResult(members["result"], members["response"], selected_contract)
    if kind == "AdmittedPluginSchemaResponse":
        return AdmittedPluginSchemaResponse(members["snapshot"], PLUGIN_SCHEMA_RESPONSE_CONTRACT)
    if kind == "PluginSchemaSnapshot":
        return PluginSchemaSnapshot(**members)
    if kind == "SchemaSecretRequirement":
        return SchemaSecretRequirement(**members)
    if kind == "JSONSchemaSnapshot":
        return JSONSchemaSnapshot(**members)
    if kind == "KnobSchemaSnapshot":
        return KnobSchemaSnapshot(**members)
    if kind == "ContinuationCallState":
        return ContinuationCallState(**members)
    if kind == "_CallWireFacts":
        return _CallWireFacts(**members)
    if kind == "BatchAccumulator":
        return BatchAccumulator(**members)
    if kind == "ContinuationBatchState":
        return ContinuationBatchState(**members)
    if kind == "_AdmittedToolCall":
        return _AdmittedToolCall(**members)
    if kind == "_AdmittedToolFunction":
        return _AdmittedToolFunction(**members)
    if kind == "_ToolOutcome":
        return _ToolOutcome(**members)
    if kind == "PrivateToolAuthoring":
        return PrivateToolAuthoring(**members)
    if kind == "ComposerMutationCeiling":
        return ComposerMutationCeiling(**members)
    if kind == "ComposerMutationRevocation":
        return ComposerMutationRevocation(**members)
    if kind == "ToolResult":
        return ToolResult(**members)
    if kind == "ValidationResult":
        checked_validation_result = ValidationResult(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_validation_result)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_validation_result.model_copy(update=members)
    if kind == "ValidationCheck":
        checked_validation_check = ValidationCheck(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_validation_check)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_validation_check.model_copy(update=members)
    if kind == "ValidationError":
        checked_validation_error = ValidationError(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_validation_error)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_validation_error.model_copy(update=members)
    if kind == "ValidationWarning":
        checked_validation_warning = ValidationWarning(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_validation_warning)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_validation_warning.model_copy(update=members)
    if kind == "SemanticEdgeContractResponse":
        checked_edge_contract = SemanticEdgeContractResponse(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_edge_contract)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_edge_contract.model_copy(update=members)
    if kind == "ValidationReadinessBlocker":
        checked_readiness_blocker = ValidationReadinessBlocker(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_readiness_blocker)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_readiness_blocker.model_copy(update=members)
    if kind == "ValidationReadiness":
        checked_readiness = ValidationReadiness(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_readiness)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_readiness.model_copy(update=members)
    if kind == "SafeRequestRejection":
        checked_request_rejection = SafeRequestRejection(**members)
        # The real constructor validates every value first. Preserve shared
        # list/model objects instead of Pydantic's container copies.
        if _constructor_shape(_members(checked_request_rejection)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_request_rejection.model_copy(update=members)
    if kind == "RetainedBatchFailure":
        return RetainedBatchFailure(**members)
    if kind == "RetainedAdvisorRefusalSource":
        return RetainedAdvisorRefusalSource(**members)
    if kind == "PluginSchemaInfo":
        checked_plugin_schema = PluginSchemaInfo(**members)
        if _constructor_shape(_members(checked_plugin_schema)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_plugin_schema.model_copy(update=members)
    if kind == "PluginSummary":
        checked_plugin_summary = PluginSummary(**members)
        if _constructor_shape(_members(checked_plugin_summary)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_plugin_summary.model_copy(update=members)
    if kind == "ConfigFieldSummary":
        checked_config_field = ConfigFieldSummary(**members)
        if _constructor_shape(_members(checked_config_field)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_config_field.model_copy(update=members)
    if kind == "PluginSecretRequirement":
        checked_secret_requirement = PluginSecretRequirement(**members)
        if _constructor_shape(_members(checked_secret_requirement)[1]) != _constructor_shape(members):
            _fail("batch constructor changed nominal types or complete values")
        return checked_secret_requirement.model_copy(update=members)
    if kind == "CapabilityDeclaration":
        return CapabilityDeclaration(**members)
    raise AuditIntegrityError("batch graph has no selected constructor")


def _encode(
    data: RestoredBatchData,
    env_ref_names: frozenset[str],
    prepared: tuple[PortableDispatchAudit, ToolContractDialect] | None = None,
    *,
    memory_completion: bool = False,
    application: ApplicationGraph | None = None,
) -> str:
    if application is not None:
        from elspeth.web.composer.continuation_application_graph import application_result_tools

        if prepared is not None or memory_completion or application.selected is not data:
            _fail("application graph mixed another cut or selected root")
        result_tools = application_result_tools(application, env_ref_names)
        if application.selected_audit is not None and application.selected_dialect is not None:
            prepared = (application.selected_audit, application.selected_dialect)
    else:
        result_tools = _joint(data, env_ref_names, prepared, memory_completion=memory_completion)
    return _encode_values(data, env_ref_names, result_tools, prepared, memory_completion=memory_completion, application=application)


def _encode_values(
    data: RestoredBatchData,
    env_ref_names: frozenset[str],
    result_tools: dict[int, str],
    prepared: tuple[PortableDispatchAudit, ToolContractDialect] | None = None,
    *,
    memory_completion: bool = False,
    application: ApplicationGraph | None = None,
) -> str:
    """Directional wire emission inside an already checked constructor call.

    This returns JSON only, never admission or authority. Source constructors
    still own complete decoding, role checks and the alias-partition comparison.
    """
    if application is not None and application.selected_audit is not None and application.selected_dialect is not None:
        prepared = application.selected_audit, application.selected_dialect
    identities: dict[int, int] = {}
    active: set[int] = set()
    nodes: list[Any] = []

    def visit(value: Any, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            _fail("batch graph exceeds its finite depth")
        if value is None or type(value) in (str, int, float, bool):
            # canonical_json rejects nonfinite and unsafe numeric values.
            canonical_json(value)
            return {"scalar": value}
        identity = id(value)
        if identity in active:
            _fail("batch data contains an unsupported cycle")
        if identity in identities:
            return {"ref": identities[identity]}
        if len(nodes) >= _MAX_NODES:
            _fail("batch graph exceeds its finite node bound")
        index = len(nodes)
        identities[identity] = index
        nodes.append(None)
        active.add(identity)
        node: dict[str, str | list[Any]]
        extra_members = None
        if application is not None:
            from elspeth.web.composer.continuation_application_graph import application_members

            extra_members = application_members(value)
        elif type(value) is ComposerToolInvocation:
            # Only the new closed refusal edge reaches an invocation in a
            # standalone batch. Reuse its exact codec/private-authoring links;
            # arbitrary application roots remain unavailable in this mode.
            from elspeth.web.composer.continuation_application_graph import application_members

            extra_members = application_members(value)
        elif type(value) is PrivateFailureFacts:
            # Crash facts are not a lawful successful-refusal edge.
            _fail("standalone refusal invocation cannot acquire crash facts")
        if extra_members is not None:
            extra_kind, extra_fields = extra_members
            node = {"kind": extra_kind, "value": [[key, visit(item, depth + 1)] for key, item in extra_fields.items()]}
        elif type(value) is PortableDispatchAudit:
            if prepared is None or value is not prepared[0]:
                _fail("portable audit is not the selected prepared call data")
            node = {"kind": "portable_dispatch_audit", "value": encode_dispatch_audit(value, env_ref_names=env_ref_names)}
        elif type(value) is CompositionState:
            encoded = canonical_json(value.to_dict())
            _state_from_tool_json(encoded, env_ref_names=env_ref_names)
            node = {"kind": "state", "value": encoded}
        elif type(value) is ValidationSummary:
            encoded = _validation_json(value)
            _decode_validation(encoded)
            _safe(_read_json_value(encoded), env_ref_names)
            node = {"kind": "validation", "value": encoded}
        elif type(value) is ToolArgumentErrorCategory:
            node = {"kind": "argument_category", "value": value.value}
        elif type(value) is AuditCharacteristic:
            node = {"kind": "AuditCharacteristic", "value": value.value}
        elif type(value) is ControlRole:
            node = {"kind": "ControlRole", "value": value.value}
        elif type(value) is PluginCapability:
            node = {"kind": "PluginCapability", "value": value.value}
        elif type(value) is WebConfigAuthority:
            node = {"kind": "WebConfigAuthority", "value": value.value}
        elif type(value) in (dict, MappingProxyType):
            if any(type(key) is not str for key in value):
                _fail("batch JSON mapping has a non-string key")
            node = {
                "kind": "dict" if type(value) is dict else "mapping",
                "value": [[key, visit(item, depth + 1)] for key, item in value.items()],
            }
        elif type(value) in (list, tuple, FrozenJsonArray):
            sequence_kind = "list" if type(value) is list else "frozen_json_array" if type(value) is FrozenJsonArray else "tuple"
            node = {"kind": sequence_kind, "value": [visit(item, depth + 1) for item in value]}
        elif type(value) in (set, frozenset):
            # These two owners contain only immutable Resource pairs. The
            # ordering of resource sets is not an execution order.
            if any(type(item) is not tuple or len(item) != 2 or any(type(part) is not str for part in item) for item in value):
                _fail("batch graph set is not a resource set")
            node = {"kind": "set" if type(value) is set else "frozenset", "value": [visit(item, depth + 1) for item in sorted(value)]}
        else:
            kind, members = _members(value, result_tools.get(identity))
            if type(value) is ContinuationCallState:
                if prepared is None or value is not data.batch.current_call:
                    _fail("call graph node is not the selected prepared call")
                # Local projection only: the live original audit/timer is not
                # mutated. Decode keeps this portable owner separately.
                members["audit"] = prepared[0]
            if type(value) is ToolResult:
                if identity not in result_tools:
                    _fail("batch ToolResult is not bound to an admitted outcome")
                # Complete B1 admission includes private flags and selected
                # response contracts. Its copy is never used to infer aliases.
                encode_tool_result_value(result_tools[identity], value, env_ref_names=env_ref_names)
                _safe(value._validation_snapshot_hash, env_ref_names)
            if type(value) in (ValidationResult, ComposerMutationRevocation, ComposerMutationCeiling):
                _safe(TypeAdapter(type(value)).dump_python(value, mode="json", warnings="error"), env_ref_names)
            if type(value) is RetainedBatchFailure:
                _safe((value.kind, value.error_class), env_ref_names)
            if type(value) is RetainedAdvisorRefusalSource:
                _safe((value.kind, value.provider_call_id, value.allowance_limit), env_ref_names)
            node = {"kind": kind, "value": [[key, visit(item, depth + 1)] for key, item in members.items()]}
        nodes[index] = node
        active.remove(identity)
        return {"ref": index}

    roots = [visit(data.batch), visit(data.accumulator), visit(data.calls)]
    payload: dict[str, Any] = {"schema_version": "composer.coupled-batch-data.v1", "roots": roots, "nodes": nodes}
    if application is not None:
        roots.extend((visit(application.effects), visit(application.recorder), visit(application.cuts)))
        payload["schema_version"] = "composer.evidence-batch-graph.v1"
    if prepared is not None:
        payload["schema_version"] = "composer.prepared-batch-data.v1"
        payload["dialect"] = prepared[1].value
        if memory_completion:
            payload["schema_version"] = "composer.memory-completion-batch-data.v1"
        elif application is not None:
            payload["schema_version"] = "composer.prepared-evidence-batch-graph.v1"
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > JSON_MAX_TOTAL_UTF8_BYTES:
        _fail("coupled batch data exceeds its complete byte bound")
    return encoded


def _decode_graph(
    encoded: str,
    env_ref_names: frozenset[str],
    *,
    prepared: bool = False,
    memory_completion: bool = False,
    application: bool = False,
) -> RestoredBatchData | RestoredPreparedBatchData | RestoredMemoryCompletionBatchData | ApplicationGraph:
    return _decode_graph_values(encoded, env_ref_names, prepared=prepared, memory_completion=memory_completion, application=application)[0]


def _decode_graph_values(
    encoded: str,
    env_ref_names: frozenset[str],
    *,
    prepared: bool = False,
    memory_completion: bool = False,
    application: bool = False,
) -> tuple[RestoredBatchData | RestoredPreparedBatchData | RestoredMemoryCompletionBatchData | ApplicationGraph, dict[int, str]]:
    if type(encoded) is not str or len(encoded.encode("utf-8")) > JSON_MAX_TOTAL_UTF8_BYTES:
        _fail("coupled batch data exceeds its complete byte bound")
    payload: Any = _read_json_value(encoded)
    if application:
        if prepared or memory_completion:
            _fail("application decoder mixed another phase format")
        prepared = type(payload) is dict and payload.get("schema_version") == "composer.prepared-evidence-batch-graph.v1"
    expected_keys = {"schema_version", "roots", "nodes", "dialect"} if prepared else {"schema_version", "roots", "nodes"}
    expected_codec = "composer.prepared-batch-data.v1" if prepared else "composer.coupled-batch-data.v1"
    if memory_completion:
        if not prepared:
            _fail("memory completion format requires its audit/dialect fields")
        expected_codec = "composer.memory-completion-batch-data.v1"
    if application:
        expected_codec = "composer.prepared-evidence-batch-graph.v1" if prepared else "composer.evidence-batch-graph.v1"
    if type(payload) is not dict or set(payload) != expected_keys or payload["schema_version"] != expected_codec:
        _fail("batch graph lost its closed format")
    dialect = None
    if prepared:
        if type(payload["dialect"]) is not str:
            _fail("prepared batch dialect is not exact text")
        try:
            dialect = ToolContractDialect(payload["dialect"])
        except ValueError as error:
            raise AuditIntegrityError("prepared batch dialect is outside its actual vocabulary") from error
    audit_values: list[PortableDispatchAudit] = []
    nodes, roots = payload["nodes"], payload["roots"]
    root_count = 6 if application else 3
    if type(nodes) is not list or len(nodes) > _MAX_NODES or type(roots) is not list or len(roots) != root_count:
        _fail("batch graph lost its finite nodes or exact coupled roots")
    restored: dict[int, Any] = {}
    active: set[int] = set()

    def visit(reference: Any, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH or type(reference) is not dict:
            _fail("batch graph has an invalid or overdeep reference")
        if set(reference) == {"scalar"}:
            scalar = reference["scalar"]
            if scalar is not None and type(scalar) not in (str, int, float, bool):
                _fail("batch scalar reference contains an object")
            return scalar
        if set(reference) != {"ref"} or type(reference["ref"]) is not int or not 0 <= reference["ref"] < len(nodes):
            _fail("batch graph has an invalid node reference")
        index = reference["ref"]
        if index in active:
            _fail("batch graph contains an unsupported cycle")
        if index in restored:
            return restored[index]
        active.add(index)
        node = nodes[index]
        if type(node) is not dict or set(node) != {"kind", "value"} or type(node["kind"]) is not str:
            _fail("batch graph lost its closed node")
        kind, value = node["kind"], node["value"]
        result: Any
        if kind == "portable_dispatch_audit":
            if not prepared or type(value) is not str:
                _fail("portable audit occurs outside a prepared data cut")
            result = decode_dispatch_audit(value, env_ref_names=env_ref_names)
        elif kind in {"AuditCharacteristic", "ControlRole", "PluginCapability", "WebConfigAuthority"}:
            if type(value) is not str:
                _fail("batch enum value is not exact text")
            if kind == "AuditCharacteristic":
                result = AuditCharacteristic(value)
            elif kind == "ControlRole":
                result = ControlRole(value)
            elif kind == "PluginCapability":
                result = PluginCapability(value)
            else:
                result = WebConfigAuthority(value)
        elif kind in {"state", "validation", "argument_category"}:
            if type(value) is not str:
                _fail("batch leaf value is not exact text")
            if kind == "state":
                result = _state_from_tool_json(value, env_ref_names=env_ref_names)
            elif kind == "validation":
                result = _decode_validation(value)
            else:
                result = ToolArgumentErrorCategory(value)
        elif kind in {"list", "tuple", "frozen_json_array", "set", "frozenset"}:
            if type(value) is not list:
                _fail("batch collection lost its ordered references")
            elements = [visit(item, depth + 1) for item in value]
            if kind == "list":
                result = elements
            elif kind == "tuple":
                result = tuple(elements)
            elif kind == "frozen_json_array":
                result = FrozenJsonArray(elements)
            elif kind == "set":
                result = set(elements)
            else:
                result = frozenset(elements)
        else:
            if type(value) is not list:
                _fail("batch object lost its ordered member pairs")
            members: dict[str, Any] = {}
            for pair in value:
                if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str or pair[0] in members:
                    _fail("batch object has a duplicate or malformed member")
                members[pair[0]] = visit(pair[1], depth + 1)
            if kind == "dict":
                result = members
            elif kind == "mapping":
                result = MappingProxyType(members)
            else:
                if kind == "ContinuationCallState":
                    if not prepared or tuple(members) != _FIELDS[kind] or type(members["audit"]) is not PortableDispatchAudit:
                        _fail("prepared call lost its complete portable audit member")
                    audit_values.append(members["audit"])
                    members["audit"] = None
                if (application and kind.startswith("application_")) or kind == "application_invocation":
                    from elspeth.web.composer.continuation_application_graph import construct_application_member

                    result = construct_application_member(kind, members)
                else:
                    result = _construct(kind, members)
        restored[index] = result
        active.remove(index)
        return result

    try:
        batch, accumulator, calls = (visit(root) for root in roots[:3])
        data = RestoredBatchData(batch, accumulator, calls)
        application_value = None
        if application:
            from elspeth.web.composer.continuation_application_graph import ApplicationGraph, application_result_tools

            effects, recorder, cuts = (visit(root) for root in roots[3:])
            if prepared:
                if len(audit_values) != 1 or dialect is None:
                    _fail("active evidence graph lost its one actual audit/dialect")
                application_value = ApplicationGraph(data, effects, recorder, cuts, audit_values[0], dialect)
            else:
                application_value = ApplicationGraph(data, effects, recorder, cuts)
            result_tools = application_result_tools(application_value, env_ref_names)
        elif prepared:
            if len(audit_values) != 1 or dialect is None:
                _fail("prepared batch lost its one exact audit/dialect owner")
            result_tools = _joint(data, env_ref_names, (audit_values[0], dialect), memory_completion=memory_completion)
        else:
            result_tools = _joint(data, env_ref_names)
    except (KeyError, TypeError, ValueError, ModelValidationError) as error:
        raise AuditIntegrityError("retained coupled batch violates its actual owned constructors") from error
    if len(restored) != len(nodes):
        _fail("batch graph contains unreachable nodes")
    if application_value is not None:
        return application_value, result_tools
    if prepared:
        if dialect is None:
            _fail("prepared batch has no dialect")
        if memory_completion:
            return RestoredMemoryCompletionBatchData(batch, accumulator, calls, audit_values[0], dialect), result_tools
        return RestoredPreparedBatchData(batch, accumulator, calls, audit_values[0], dialect), result_tools
    return data, result_tools


def _decode(encoded: str, env_ref_names: frozenset[str]) -> RestoredBatchData:
    data = _decode_graph(encoded, env_ref_names)
    if type(data) is not RestoredBatchData:
        _fail("quiescent decoder returned another data owner")
    return data


def encode_coupled_batch_data(
    *,
    batch: ContinuationBatchState,
    accumulator: BatchAccumulator,
    calls: tuple[_AdmittedToolCall, ...],
    env_ref_names: frozenset[str] = frozenset(),
) -> str:
    """Retain an actual quiescent data cut; refuse unsupported authority cuts."""
    encoded = _encode(RestoredBatchData(batch, accumulator, calls), env_ref_names)
    restored, result_tools = _decode_graph_values(encoded, env_ref_names)
    if type(restored) is not RestoredBatchData:
        _fail("quiescent encoder restored another phase owner")
    # Ref IDs are allocated by identity, not value equality. This compares the
    # complete alias partition, field order, and node kinds as well as values.
    if _encode_values(restored, env_ref_names, result_tools) != encoded:
        _fail("actual constructors cannot preserve this batch alias topology")
    return encoded


def decode_coupled_batch_data(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> RestoredBatchData:
    """Return data owners only; no current fence or executable context is made."""
    restored, result_tools = _decode_graph_values(encoded, env_ref_names)
    if type(restored) is not RestoredBatchData:
        _fail("quiescent decoder restored another phase owner")
    if _encode_values(restored, env_ref_names, result_tools) != encoded:
        _fail("retained batch lost its exact values or object alias partition")
    return restored


def encode_coupled_batch_chunk(
    *,
    batch: ContinuationBatchState,
    accumulator: BatchAccumulator,
    calls: tuple[_AdmittedToolCall, ...],
    env_ref_names: frozenset[str] = frozenset(),
) -> PrivateChunk:
    canonical = encode_coupled_batch_data(batch=batch, accumulator=accumulator, calls=calls, env_ref_names=env_ref_names)
    return encode_private_chunk(codec="composer.coupled-batch-data.v1", canonical=canonical, env_ref_names=env_ref_names)


def decode_coupled_batch_chunk(chunk: PrivateChunk, *, env_ref_names: frozenset[str] = frozenset()) -> RestoredBatchData:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.coupled-batch-data.v1":
        raise AuditIntegrityError("coupled batch chunk has no exact selected codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    return decode_coupled_batch_data(chunk.canonical, env_ref_names=env_ref_names)


def _check_prepared(
    data: RestoredBatchData,
    retained: PortableDispatchAudit,
    dialect: ToolContractDialect,
    env_ref_names: frozenset[str],
    *,
    memory_completion: bool = False,
) -> None:
    """Recheck the actual prepare-stage data joins without creating a timer."""
    batch = data.batch
    call = batch.current_call
    if type(call) is not ContinuationCallState or type(dialect) is not ToolContractDialect:
        _fail("prepared cut requires its actual call and dialect owners")
    if (
        type(call.index) is not int
        or call.index != batch.next_tool_index
        or not 0 <= call.index < len(data.calls)
        or call.tool_call is not data.calls[call.index]
    ):
        _fail("prepared call lost its admitted call/cursor identity")
    expected_phase = "application_pending" if memory_completion else "prepared"
    if call.phase != expected_phase or batch.stop_issued_batch:
        _fail("prepared cut is outside the before-execution phase")
    if call.pre_state is not batch.state or type(call.pre_version) is not int or call.pre_version != call.pre_state.version:
        _fail("prepared call lost the actual pre-state identity/version")
    if call.tool_name != call.tool_call.function.name or call.private_raw_arguments != call.tool_call.function.arguments:
        _fail("prepared call lost its exact selected raw call")
    if type(call.wire) is not _CallWireFacts or any(
        value is not None and type(value) is not bool for value in (call.wire.strict_sent, call.wire.wire_conformant)
    ):
        _fail("prepared call lost its actual wire facts")
    if type(call.arguments) is not dict or type(call.audit_arguments) is not dict:
        _fail("prepared cut requires complete actual semantic/audit argument objects")
    if call.unknown_audit_arguments is not None or call.tool_name not in _WIRE_TOOL_DEFS[dialect]:
        _fail("prepared cut requires a tool admitted by the retained sent dialect")
    if memory_completion:
        if any(
            value is not None
            for value in (
                call.effect_key,
                call.owner_result_kind,
                call.owner_result_json,
                call.completion_json,
                call.advisor_invocation_json,
            )
        ):
            _fail("memory completion contains a separate SQL/Advisor owner cut")
    elif any(
        value is not None
        for value in (
            call.effect_key,
            call.owner_result_kind,
            call.owner_result_json,
            call.memory_result,
            call.completion_json,
            call.owner_tool_receipt_json,
            call.advisor_invocation_json,
        )
    ):
        _fail("prepared cut contains a later owner result or receipt requiring its own admission")
    if (call.original_creator_id is None) != (call.original_creator_epoch is None):
        _fail("prepared call lost its paired historical creator provenance")
    if call.original_creator_id is not None and (
        type(call.original_creator_id) is not str
        or not call.original_creator_id
        or type(call.original_creator_epoch) is not int
        or call.original_creator_epoch < 1
    ):
        _fail("prepared call has invalid historical creator provenance")
    _safe(call.original_creator_id, env_ref_names)
    parsed = bounded_json_loads(call.private_raw_arguments, label="retained prepared call")
    if type(parsed) is not dict:
        _fail("prepared raw call is not the actual object admitted before execution")
    decoded = decode_wire_arguments(call.tool_name, dialect, parsed)
    semantic = owned_json(decoded.semantic)
    if type(semantic) is not dict:
        _fail("actual prepared wire decoder returned a non-object semantic value")
    expected_strict = _WIRE_TOOL_DEFS[dialect][call.tool_name].strict_capable if dialect is ToolContractDialect.OPENAI_STRICT else None
    if call.wire.strict_sent is not expected_strict or call.wire.wire_conformant is not decoded.wire_conformant:
        _fail("prepared call wire facts differ from the actual retained dialect")
    if canonical_json(semantic) != canonical_json(call.arguments):
        _fail("prepared semantic arguments differ from actual wire decoding")
    if call.private_arguments_canonical != canonical_json(call.arguments):
        _fail("prepared call lost its private semantic canonical bytes")
    if call.tool_name == "set_pipeline":
        if composer_authority_canonical_json(semantic) != composer_authority_canonical_json(
            call.arguments
        ) or call.private_authority_canonical != composer_authority_canonical_json(call.arguments):
            _fail("prepared call lost its order-bearing source argument binding")
        expected_audit = inline_custody_audit_projection(call.arguments)
        if call.audit_arguments is call.arguments or composer_authority_canonical_json(
            call.audit_arguments
        ) != composer_authority_canonical_json(expected_audit):
            _fail("prepared call lost its separate actual custody audit projection")
    elif call.private_authority_canonical is not None or call.audit_arguments is not call.arguments:
        _fail("prepared call lost its actual semantic/audit argument alias")
    if batch.decoded_args_by_call_id.get(call.tool_call.id) is not call.audit_arguments:
        _fail("prepared call lost its retained decoded-argument alias")
    authoring = batch.private_authorings_by_call[call.tool_call.id]
    if (
        authoring.arguments_canonical != call.private_arguments_canonical
        or authoring.authority_arguments_canonical != call.private_authority_canonical
    ):
        _fail("prepared call private authoring differs from its exact semantic facts")
    require_retained_argument_credentials(call.tool_name, owned_json(call.arguments), env_ref_names=env_ref_names)
    require_retained_argument_credentials(call.tool_name, owned_json(call.audit_arguments), env_ref_names=env_ref_names)
    # This is historical audit DATA admission, not a fresh current-context
    # authorization. The actual begin_dispatch owner is invoked only at the
    # explicit re-admission seam below.
    encode_dispatch_audit(retained, env_ref_names=env_ref_names)
    if (
        retained.tool_call_id != call.tool_call.id
        or retained.tool_name != call.tool_name
        or retained.version_before != call.pre_version
        or retained.arguments_canonical != canonical_json(call.audit_arguments)
        or retained.strict_sent is not call.wire.strict_sent
        or retained.wire_conformant is not call.wire.wire_conformant
    ):
        _fail("portable audit differs from its selected prepared call")
    expected_authority = composer_authority_canonical_json(call.audit_arguments) if call.tool_name == "set_pipeline" else None
    if retained.authority_arguments_canonical != expected_authority:
        _fail("portable audit differs from its actual order-bearing audit arguments")
    if call.audit is not None:
        if type(call.audit) is not DispatchAudit:
            _fail("prepared live audit is not its actual owner")
        from elspeth.web.composer.continuation_dispatch_audit import _binding

        if _binding(call.audit) != _binding(retained) or call.audit.started_at != retained.started_at:
            _fail("portable audit differs from the untouched original live audit")


def encode_prepared_batch_data(
    *,
    batch: ContinuationBatchState,
    accumulator: BatchAccumulator,
    calls: tuple[_AdmittedToolCall, ...],
    dialect: ToolContractDialect,
    env_ref_names: frozenset[str] = frozenset(),
) -> str:
    """Capture a real before-execution cut once; never mutate the live timer."""
    if (
        type(batch) is not ContinuationBatchState
        or type(batch.current_call) is not ContinuationCallState
        or type(batch.current_call.audit) is not DispatchAudit
    ):
        _fail("prepared capture requires its actual live dispatch audit")
    retained = capture_dispatch_audit(batch.current_call.audit, env_ref_names=env_ref_names)
    encoded = _encode(RestoredBatchData(batch, accumulator, calls), env_ref_names, (retained, dialect))
    restored = decode_prepared_batch_data(encoded, env_ref_names=env_ref_names)
    if encode_restored_prepared_batch_data(restored, env_ref_names=env_ref_names) != encoded:
        _fail("actual constructors cannot preserve the prepared batch alias topology")
    return encoded


def encode_restored_prepared_batch_data(
    value: RestoredPreparedBatchData,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> str:
    """Re-encode historical DATA without capturing new elapsed time."""
    if type(value) is not RestoredPreparedBatchData:
        raise TypeError("prepared data requires its actual returned owner")
    if type(value.batch.current_call) is not ContinuationCallState or value.batch.current_call.audit is not None:
        _fail("restored DATA must keep the portable audit separate until readmission")
    return _encode(RestoredBatchData(value.batch, value.accumulator, value.calls), env_ref_names, (value.audit, value.dialect))


def decode_prepared_batch_data(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> RestoredPreparedBatchData:
    """Restore complete data roots; do not silently mint a runnable audit."""
    restored = _decode_graph(encoded, env_ref_names, prepared=True)
    if type(restored) is not RestoredPreparedBatchData:
        _fail("prepared decoder returned another data owner")
    if encode_restored_prepared_batch_data(restored, env_ref_names=env_ref_names) != encoded:
        _fail("retained prepared data lost its exact values or alias partition")
    return restored


def readmit_prepared_batch_audit(
    value: RestoredPreparedBatchData,
    *,
    selected_call: _AdmittedToolCall,
    selected_pre_state: CompositionState,
    current_dialect: ToolContractDialect,
    actor: str,
    strict_sent: bool | None,
    wire_conformant: bool | None,
    env_ref_names: frozenset[str] = frozenset(),
) -> DispatchAudit:
    """Return only the real freshly bound audit; do not attach or authorize it.

    The current F7 caller must first admit its checkpoint/context/authorization
    and supply the selected exact graph roots. Historical creator fields are
    never current authority. That absent service integration is deliberately
    not inferred from this pure-data function. After this check, that caller
    owns attaching the returned audit and invoking the ordinary live helper,
    whose closure is constructed from the actual call/batch at execution.
    """
    encode_restored_prepared_batch_data(value, env_ref_names=env_ref_names)
    call = value.batch.current_call
    if type(call) is not ContinuationCallState or type(current_dialect) is not ToolContractDialect:
        _fail("prepared readmission requires actual selected owners")
    if selected_call is not call.tool_call or selected_pre_state is not call.pre_state or current_dialect is not value.dialect:
        _fail("prepared readmission differs from its exact selected checkpoint roots/dialect")
    if type(actor) is not str or strict_sent is not call.wire.strict_sent or wire_conformant is not call.wire.wire_conformant:
        _fail("prepared readmission differs from its actual actor/wire fact types")
    if call.audit_arguments is None:
        _fail("prepared readmission lost its complete audit arguments")
    return re_admit_dispatch_audit(
        value.audit,
        selected_call=selected_call,
        pre_state=selected_pre_state,
        audit_arguments=call.audit_arguments,
        actor=actor,
        strict_sent=strict_sent,
        wire_conformant=wire_conformant,
        env_ref_names=env_ref_names,
    )


def _check_memory_completion(
    data: RestoredBatchData,
    audit: PortableDispatchAudit,
    env_ref_names: frozenset[str],
    result_tools: dict[int, str],
) -> ComposerToolInvocation:
    call = data.batch.current_call
    if type(call) is not ContinuationCallState or type(call.owner_tool_receipt_json) is not str:
        _fail("memory completion lost its actual receipt")
    tool_name = call.tool_name
    if type(tool_name) is not str:
        _fail("memory completion lost its exact selected tool name")
    result = call.memory_result
    if type(result) is AdmittedDiscoveryResult:
        contract = response_contract_for(tool_name)
        if contract is None or result.contract is not contract or type(result.result) is not ToolResult:
            _fail("memory discovery differs from its actual selected nominal owner")
        # Failure envelopes have no admitted payload. Successful snapshot
        # retention is closed to this fully represented nominal family.
        if result.result.success:
            if (
                tool_name != "get_plugin_schema"
                or contract is not PLUGIN_SCHEMA_RESPONSE_CONTRACT
                or type(result.response) is not AdmittedPluginSchemaResponse
                or result.response.contract is not contract
                or type(result.response.snapshot) is not PluginSchemaSnapshot
            ):
                _fail("memory discovery snapshot requires its own complete graph owner")
            readmitted = result.response.readmit(contract)
            if canonical_json(readmitted.to_wire()) != canonical_json(result.response.to_wire()):
                _fail("memory discovery snapshot changed under actual readmission")
        elif result.response is not None:
            _fail("failed memory discovery invented an admitted payload")
        admitted = admit_discovery_result(tool_name, result.result)
        if canonical_json(admitted.to_dict()) != canonical_json(result.to_dict()):
            _fail("memory discovery differs from its actual selected result")
        result_tools[id(result)] = tool_name
        nominal = result.result
    elif type(result) is ToolResult:
        nominal = result
    else:
        _fail("memory completion lost its actual nominal result")
    if id(nominal) in result_tools and result_tools[id(nominal)] != tool_name:
        _fail("one retained ToolResult cannot have two selected tool contracts")
    result_tools[id(nominal)] = tool_name
    receipt = decode_retained_tool_receipt(call.owner_tool_receipt_json, env_ref_names=env_ref_names)
    if receipt.effect_key != f"memory:{call.tool_call.id}":
        _fail("memory completion receipt belongs to another effect")
    invocation = require_receipt_result(
        receipt, selected_call=call.tool_call, pre_state=call.pre_state, result=result, env_ref_names=env_ref_names
    )
    if invocation.status is not ComposerToolStatus.SUCCESS:
        _fail("memory result cut is not an actual completed dispatch")
    if invocation.private_authoring != data.batch.private_authorings_by_call[call.tool_call.id]:
        _fail("memory completion changed its actual retained private authoring")
    if (
        invocation.tool_call_id != audit.tool_call_id
        or invocation.tool_name != audit.tool_name
        or invocation.arguments_canonical != audit.arguments_canonical
        or invocation.arguments_hash != audit.arguments_hash
        or invocation.authority_arguments_canonical != audit.authority_arguments_canonical
        or invocation.authority_arguments_hash != audit.authority_arguments_hash
        or invocation.started_at != audit.started_at
        or invocation.version_before != audit.version_before
        or invocation.actor != audit.actor
        or invocation.strict_sent is not audit.strict_sent
        or invocation.wire_conformant is not audit.wire_conformant
    ):
        _fail("memory completion differs from its actual retained audit binding")
    return invocation


def encode_memory_completion_batch_data(
    *,
    batch: ContinuationBatchState,
    accumulator: BatchAccumulator,
    calls: tuple[_AdmittedToolCall, ...],
    dialect: ToolContractDialect,
    env_ref_names: frozenset[str] = frozenset(),
) -> str:
    """Capture an already completed result; never finish or mutate its audit."""
    if (
        type(batch) is not ContinuationBatchState
        or type(batch.current_call) is not ContinuationCallState
        or type(batch.current_call.audit) is not DispatchAudit
    ):
        _fail("memory completion capture requires its original actual dispatch audit")
    retained = capture_dispatch_audit(batch.current_call.audit, env_ref_names=env_ref_names)
    encoded = _encode(RestoredBatchData(batch, accumulator, calls), env_ref_names, (retained, dialect), memory_completion=True)
    decode_memory_completion_batch_data(encoded, env_ref_names=env_ref_names)
    return encoded


def encode_restored_memory_completion_batch_data(
    value: RestoredMemoryCompletionBatchData,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> str:
    if type(value) is not RestoredMemoryCompletionBatchData:
        raise TypeError("memory completion requires its actual restored DATA owner")
    if type(value.batch.current_call) is not ContinuationCallState or value.batch.current_call.audit is not None:
        _fail("restored completed DATA must not acquire a live dispatch timer")
    return _encode(
        RestoredBatchData(value.batch, value.accumulator, value.calls), env_ref_names, (value.audit, value.dialect), memory_completion=True
    )


def decode_memory_completion_batch_data(
    encoded: str,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> RestoredMemoryCompletionBatchData:
    restored = _decode_graph(encoded, env_ref_names, prepared=True, memory_completion=True)
    if type(restored) is not RestoredMemoryCompletionBatchData:
        _fail("memory completion decoder returned another DATA owner")
    if encode_restored_memory_completion_batch_data(restored, env_ref_names=env_ref_names) != encoded:
        _fail("retained memory completion lost complete values or its alias partition")
    return restored


def require_selected_memory_completion(
    value: RestoredMemoryCompletionBatchData,
    *,
    selected_call: _AdmittedToolCall,
    selected_pre_state: CompositionState,
    current_dialect: ToolContractDialect,
    env_ref_names: frozenset[str],
) -> ComposerToolInvocation:
    """Return historical invocation only after exact selected DATA joins.

    The F7 caller must separately obtain fresh current SQL/evidence/inventory
    admission. This function creates no authorization, witness, callback or
    timer. The original completed invocation must never be finished again.
    """
    encode_restored_memory_completion_batch_data(value, env_ref_names=env_ref_names)
    call = value.batch.current_call
    if (
        type(call) is not ContinuationCallState
        or selected_call is not call.tool_call
        or selected_pre_state is not call.pre_state
        or current_dialect is not value.dialect
    ):
        _fail("memory application differs from its exact selected DATA roots/dialect")
    return _check_memory_completion(RestoredBatchData(value.batch, value.accumulator, value.calls), value.audit, env_ref_names, {})
