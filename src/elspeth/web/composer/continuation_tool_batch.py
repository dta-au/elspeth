"""Pure complete ToolResult retention; no batch runtime or read authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer._compose_loop_carriers import _ToolOutcome
from elspeth.web.composer.audit import _normalize_audit_payload
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_result_credentials import restore_retained_plugin_schema_data
from elspeth.web.composer.discovery_response import admit_discovery_result, require_discovery_result_data
from elspeth.web.composer.response_contracts import ResponseContract
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tool_result_envelope import ValidationGuidance
from elspeth.web.composer.tools._common import AppliedComponentEcho, ToolResult
from elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT
from elspeth.web.composer.tools._registry import ASYNC_TOOL_EFFECTS, response_contract_for
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.execution.schemas import ValidationResult


class ToolResultValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.tool-result.v1"] = "composer.tool-result.v1"
    tool_name: str
    success: bool
    state_json: str
    validation_json: str
    affected_nodes: tuple[str, ...]
    data_json: str | None
    prior_validation_json: str | None
    runtime_preflight_json: str | None
    post_call_hints: tuple[str, ...]
    plugin_schemas_json: str | None
    validation_guidance_json: str | None
    applied_component_json: str | None
    validation_snapshot_hash: str | None
    state_validation_withheld: bool


_VALIDATION = TypeAdapter(ValidationSummary)
_RUNTIME = TypeAdapter(ValidationResult)
_GUIDANCE = TypeAdapter(ValidationGuidance)
_SCHEMAS = TypeAdapter(dict[str, dict[str, JsonValue]])
_APPLIED = TypeAdapter(AppliedComponentEcho)


def _read_json_value(encoded: str) -> JsonValue:
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("tool continuation value is not complete canonical JSON")
    return owned_json(value)


def _optional_json(value: Any) -> str | None:
    return None if value is None else canonical_json(owned_json(deep_thaw(value)))


def _dump_owned[T](adapter: TypeAdapter[T], value: T) -> str:
    return canonical_json(owned_json(adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json)))


def _decode_owned[T](adapter: TypeAdapter[T], encoded: str) -> T:
    _read_json_value(encoded)
    try:
        result = adapter.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation member violates its owned contract") from error
    if _dump_owned(adapter, result) != encoded:
        raise AuditIntegrityError("tool continuation member lost or normalized fields")
    return result


def _validation_json(value: ValidationSummary) -> str:
    if type(value) is not ValidationSummary:
        raise TypeError("tool validation requires its nominal summary")
    payload = _VALIDATION.dump_python(value, mode="json", warnings="error", fallback=owned_json)
    # These owned fields are sets, unlike all ordered errors/edges/hints.
    # Their wire order cannot depend on the encoder process's hash seed.
    for contract in payload["semantic_contracts"]:
        requirement = contract["requirement"]
        for field in ("accepted_content_kinds", "accepted_text_framings", "accepted_value_types"):
            requirement[field] = sorted(requirement[field])
    return canonical_json(owned_json(payload))


def _decode_validation(encoded: str) -> ValidationSummary:
    _read_json_value(encoded)
    try:
        result = _VALIDATION.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation validation violates its owned contract") from error
    if _validation_json(result) != encoded:
        raise AuditIntegrityError("tool continuation validation lost or normalized fields")
    return result


def _state_from_tool_json(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> CompositionState:
    value = _read_json_value(encoded)
    if type(value) is not dict:
        raise AuditIntegrityError("tool continuation state is not its owned object")
    if "version" not in value or type(value["version"]) is not int or value["version"] < 1:
        raise AuditIntegrityError("tool continuation state lost its exact positive version")
    try:
        state = CompositionState.from_dict(value)
    except (KeyError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation state violates its owned contract") from error
    if canonical_json(state.to_dict()) != encoded:
        raise AuditIntegrityError("tool continuation state lost fields during decode")
    require_no_credential_material_in_state(state, surface="continuation tool state", env_ref_names=env_ref_names)
    return state


def _response_contract(tool_name: str) -> ResponseContract | None:
    if tool_name in ASYNC_TOOL_EFFECTS:
        return None
    try:
        return response_contract_for(tool_name)
    except KeyError as error:
        raise AuditIntegrityError("tool continuation result has no registered tool") from error


def encode_tool_result_value(
    tool_name: str,
    result: ToolResult,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> ToolResultValue:
    if type(result) is not ToolResult or type(result.updated_state) is not CompositionState:
        raise TypeError("tool continuation codec requires the actual nominal result and state")
    state_json = canonical_json(result.updated_state.to_dict())
    _state_from_tool_json(state_json, env_ref_names=env_ref_names)
    contract = _response_contract(tool_name)
    if contract is not None:
        admitted = admit_discovery_result(tool_name, result)
        data = None if admitted.response is None else admitted.response.to_wire()
    else:
        data = None if result.data is None else owned_json(_normalize_audit_payload(deep_thaw(result.data)))
    value = ToolResultValue(
        tool_name=tool_name,
        success=result.success,
        state_json=state_json,
        validation_json=_validation_json(result.validation),
        affected_nodes=result.affected_nodes,
        data_json=_optional_json(data),
        prior_validation_json=_validation_json(result.prior_validation) if result.prior_validation is not None else None,
        runtime_preflight_json=_dump_owned(_RUNTIME, result.runtime_preflight) if result.runtime_preflight is not None else None,
        post_call_hints=result.post_call_hints,
        plugin_schemas_json=_optional_json(_read_json_value(canonical_json(result.plugin_schemas))),
        validation_guidance_json=_optional_json(result.validation_guidance),
        applied_component_json=_optional_json(result.applied_component),
        validation_snapshot_hash=result._validation_snapshot_hash,
        state_validation_withheld=result._state_validation_withheld,
    )
    # Refuse a value that this build cannot restore completely; no lossy fallback.
    decode_tool_result_value(value, pre_state=result.updated_state, env_ref_names=env_ref_names)
    return value


def decode_tool_result_value(
    value: ToolResultValue,
    *,
    pre_state: CompositionState | None = None,
    env_ref_names: frozenset[str] = frozenset(),
) -> ToolResult:
    if type(value) is not ToolResultValue:
        raise TypeError("tool result decoder requires its closed owned value")
    # model_copy/model_construct can bypass a model's validation.
    try:
        admitted = ToolResultValue.model_validate_json(canonical_json(value.model_dump(mode="json", warnings="error")), strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation value violates its closed model") from error
    restored = _state_from_tool_json(admitted.state_json, env_ref_names=env_ref_names)
    if pre_state is not None and type(pre_state) is not CompositionState:
        raise TypeError("tool result pre-state must be its nominal state")
    state = pre_state if pre_state is not None and canonical_json(pre_state.to_dict()) == admitted.state_json else restored
    contract = _response_contract(admitted.tool_name)
    if contract is not None:
        require_discovery_result_data(
            success=admitted.success,
            data=None if admitted.data_json is None else _read_json_value(admitted.data_json),
        )
    data: dict[str, JsonValue] | list[JsonValue] | PluginSchemaInfo | None
    if admitted.data_json is None:
        data = None
    else:
        parsed_data = _read_json_value(admitted.data_json)
        if not isinstance(parsed_data, dict | list):
            raise AuditIntegrityError("tool result data is not its owned object or array")
        data = (
            restore_retained_plugin_schema_data(parsed_data, env_ref_names=env_ref_names)
            if contract is PLUGIN_SCHEMA_RESPONSE_CONTRACT and admitted.success
            else parsed_data
        )
    result = ToolResult(
        success=admitted.success,
        updated_state=state,
        validation=_decode_validation(admitted.validation_json),
        affected_nodes=admitted.affected_nodes,
        data=data,
        prior_validation=_decode_validation(admitted.prior_validation_json) if admitted.prior_validation_json is not None else None,
        runtime_preflight=_decode_owned(_RUNTIME, admitted.runtime_preflight_json) if admitted.runtime_preflight_json is not None else None,
        post_call_hints=admitted.post_call_hints,
        plugin_schemas=_decode_owned(_SCHEMAS, admitted.plugin_schemas_json) if admitted.plugin_schemas_json is not None else None,
        validation_guidance=_decode_owned(_GUIDANCE, admitted.validation_guidance_json)
        if admitted.validation_guidance_json is not None
        else None,
        applied_component=_decode_owned(_APPLIED, admitted.applied_component_json) if admitted.applied_component_json is not None else None,
        _validation_snapshot_hash=admitted.validation_snapshot_hash,
        _state_validation_withheld=admitted.state_validation_withheld,
    )
    if contract is not None:
        discovery = admit_discovery_result(admitted.tool_name, result)
        selected_data = None if discovery.response is None else discovery.response.to_wire()
        if _optional_json(selected_data) != admitted.data_json:
            raise AuditIntegrityError("retained discovery data changed under its selected response contract")
    return result


def decode_tool_result_json(
    encoded: str,
    *,
    pre_state: CompositionState | None = None,
    env_ref_names: frozenset[str] = frozenset(),
) -> ToolResult:
    parsed = _read_json_value(encoded)
    try:
        value = ToolResultValue.model_validate_json(canonical_json(parsed), strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("retained tool result violates its complete owned codec") from error
    if canonical_json(value.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("retained tool result lost fields during decode")
    return decode_tool_result_value(value, pre_state=pre_state, env_ref_names=env_ref_names)


@dataclass(frozen=True, slots=True)
class RetainedAdvisorRefusalSource:
    """Original successful refusal owners; DATA never grants completion."""

    kind: Literal["context_unsupported", "provider_cost_refused", "prompt_guard_refused"]
    outcome: _ToolOutcome
    invocation: ComposerToolInvocation
    provider_call_id: str | None
    allowance_limit: int | None = None

    def __post_init__(self) -> None:
        if (
            self.kind not in ("context_unsupported", "provider_cost_refused", "prompt_guard_refused")
            or type(self.outcome) is not _ToolOutcome
            or type(self.invocation) is not ComposerToolInvocation
            or (self.provider_call_id is not None and (type(self.provider_call_id) is not str or not self.provider_call_id))
            or (self.kind != "provider_cost_refused" and self.provider_call_id is not None)
            or (self.kind == "context_unsupported" and (type(self.allowance_limit) is not int or self.allowance_limit < 0))
            or (self.kind != "context_unsupported" and self.allowance_limit is not None)
        ):
            raise AuditIntegrityError("retained Advisor refusal lost its closed original owners")


@dataclass(frozen=True, slots=True)
class RetainedBatchFailure:
    """Closed graph DATA; nested live owners use their explicit graph codec."""

    kind: Literal["plugin_crash", "advisor_failure", "advisor_timeout"]
    error_class: str
    refusal_source: RetainedAdvisorRefusalSource | None = None

    def __post_init__(self) -> None:
        kind: object = self.kind
        if (
            type(kind) is not str
            or kind not in ("plugin_crash", "advisor_failure", "advisor_timeout")
            or type(self.error_class) is not str
            or not 1 <= len(self.error_class) <= 128
            or (
                self.refusal_source is not None
                and (type(self.refusal_source) is not RetainedAdvisorRefusalSource or self.kind != "advisor_failure")
            )
        ):
            raise AuditIntegrityError("retained batch failure lost its complete closed descriptor")
