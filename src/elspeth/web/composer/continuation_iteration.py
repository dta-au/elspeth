"""Pure F9 iteration history; retained inputs confer no prompt trust or current authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError

from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _AdvisorReviewState
from elspeth.web.composer.anti_anchor import AntiAnchorSnapshot
from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_result_credentials import require_retained_result_credentials
from elspeth.web.composer.continuation_tool_batch import (
    _decode_owned,
    _decode_validation,
    _dump_owned,
    _state_from_tool_json,
    _validation_json,
)
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.schema_disclosure import SchemaDisclosureSnapshot
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT, AdmittedPluginSchemaResponse
from elspeth.web.credential_guard import require_no_credential_material

_REVIEW = TypeAdapter(_AdvisorReviewState)
_FAILURE = TypeAdapter(FailedTurnMetadata)


class RetainedSchemaPromptInput(ContinuationValue):
    arguments_json: str
    raw_arguments: str
    state_index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1)


class ContinuationProducerInputs(ContinuationValue):
    # A shelf records actual state identity, never equality-derived aliasing.
    states_json: tuple[str, ...]
    schema_inputs: tuple[RetainedSchemaPromptInput, ...] = Field(alias="schema")


def producer_input_states(inputs: ContinuationProducerInputs, *, env_ref_names: frozenset[str]) -> tuple[CompositionState, ...]:
    if type(inputs) is not ContinuationProducerInputs:
        raise TypeError("schema input retention requires its exact DATA owner")
    states = tuple(_state_from_tool_json(text, env_ref_names=env_ref_names) for text in inputs.states_json)
    used: set[int] = set()
    for item in inputs.schema_inputs:
        arguments, error = parse_json_strict(item.arguments_json)
        if error is not None or type(arguments) is not dict or canonical_json(arguments) != item.arguments_json:
            raise AuditIntegrityError("schema producer input lost its complete argument object")
        raw, raw_error = parse_json_strict(item.raw_arguments)
        if raw_error is not None or canonical_json(raw) != item.arguments_json:
            raise AuditIntegrityError("schema producer raw arguments differ from the selected semantic object")
        if item.state_index >= len(states):
            raise AuditIntegrityError("schema producer input has no retained state")
        used.add(item.state_index)
        require_no_credential_material(arguments, surface="schema prompt arguments", env_ref_names=env_ref_names)
        require_no_credential_material(item.tool_call_id, surface="schema prompt call identity", env_ref_names=env_ref_names)
    if used != set(range(len(states))):
        raise AuditIntegrityError("schema input shelf contains an unreferenced state")
    return states


def capture_continuation_producer_inputs(
    inputs: tuple[tuple[_AdmittedToolCall, CompositionState], ...],
    *,
    env_ref_names: frozenset[str],
) -> ContinuationProducerInputs:
    """Capture actual selected schema-call inputs, without executing or admitting a current producer."""
    if type(inputs) is not tuple:
        raise TypeError("schema input capture requires its ordered complete cohort")
    states: list[CompositionState] = []
    retained: list[RetainedSchemaPromptInput] = []
    for item in inputs:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("schema input capture requires actual call/state pairs")
        call, state = item
        if type(call) is not _AdmittedToolCall or type(call.function) is not _AdmittedToolFunction or type(state) is not CompositionState:
            raise TypeError("schema input capture lost its actual nominal input")
        if call.function.name != "get_plugin_schema":
            raise AuditIntegrityError("schema input capture selected another tool")
        arguments, error = parse_json_strict(call.function.arguments)
        if error is not None or type(arguments) is not dict:
            raise AuditIntegrityError("schema producer requires an actual decoded argument object")
        matching = tuple(index for index, prior in enumerate(states) if prior is state)
        if matching:
            index = matching[0]
        else:
            index = len(states)
            states.append(state)
        retained.append(
            RetainedSchemaPromptInput(
                arguments_json=canonical_json(arguments), raw_arguments=call.function.arguments, state_index=index, tool_call_id=call.id
            )
        )
    result = ContinuationProducerInputs(states_json=tuple(canonical_json(state.to_dict()) for state in states), schema=tuple(retained))
    producer_input_states(result, env_ref_names=env_ref_names)
    return result


class ContinuationIterationSnapshot(ContinuationValue):
    schema_version: Literal["composer.iteration.v1"] = "composer.iteration.v1"
    producer_inputs: ContinuationProducerInputs
    last_validation_json: str | None
    advisor_review_json: str
    failed_turn_json: str | None
    anti_anchor: AntiAnchorSnapshot
    disclosure: SchemaDisclosureSnapshot


@dataclass(frozen=True, slots=True)
class ContinuationIterationValues:
    last_validation: ValidationSummary | None
    advisor_review: _AdvisorReviewState
    failed_turn: FailedTurnMetadata | None
    schema_states: tuple[CompositionState, ...]


def iteration_values(
    snapshot: ContinuationIterationSnapshot, *, env_ref_names: frozenset[str] = frozenset()
) -> ContinuationIterationValues:
    if type(snapshot) is not ContinuationIterationSnapshot:
        raise TypeError("iteration restore requires its exact complete DATA owner")
    encoded = canonical_json(snapshot.model_dump(mode="json", by_alias=True))
    try:
        checked = ContinuationIterationSnapshot.model_validate_json(encoded)
    except ValidationError as validation_error:
        raise AuditIntegrityError("iteration violates its complete closed DATA contract") from validation_error
    if canonical_json(checked.model_dump(mode="json", by_alias=True)) != encoded:
        raise AuditIntegrityError("iteration omitted or normalized a field")
    states = producer_input_states(snapshot.producer_inputs, env_ref_names=env_ref_names)
    validation = _decode_validation(snapshot.last_validation_json) if snapshot.last_validation_json is not None else None
    review = _decode_owned(_REVIEW, snapshot.advisor_review_json)
    failure = _decode_owned(_FAILURE, snapshot.failed_turn_json) if snapshot.failed_turn_json is not None else None
    # Strings containing embedded JSON are decoded before credential admission.
    for text in (snapshot.last_validation_json, snapshot.advisor_review_json, snapshot.failed_turn_json):
        if text is not None:
            value, error = parse_json_strict(text)
            if error is not None:
                raise AuditIntegrityError("iteration member is not strict JSON")
            require_no_credential_material(value, surface="iteration history", env_ref_names=env_ref_names)
    require_no_credential_material(snapshot.anti_anchor.model_dump(mode="json"), surface="anti-anchor history", env_ref_names=env_ref_names)
    require_no_credential_material(
        snapshot.disclosure.model_dump(mode="json"), surface="schema disclosure history", env_ref_names=env_ref_names
    )
    return ContinuationIterationValues(validation, review, failure, states)


def capture_continuation_iteration(
    *,
    producer_inputs: ContinuationProducerInputs,
    last_validation: ValidationSummary | None,
    advisor_review: _AdvisorReviewState,
    failed_turn: FailedTurnMetadata | None,
    anti_anchor: AntiAnchorSnapshot,
    disclosure: SchemaDisclosureSnapshot,
    env_ref_names: frozenset[str] = frozenset(),
) -> ContinuationIterationSnapshot:
    if (
        type(advisor_review) is not _AdvisorReviewState
        or type(anti_anchor) is not AntiAnchorSnapshot
        or type(disclosure) is not SchemaDisclosureSnapshot
        or (last_validation is not None and type(last_validation) is not ValidationSummary)
        or (failed_turn is not None and type(failed_turn) is not FailedTurnMetadata)
    ):
        raise TypeError("iteration capture requires actual owned phase values")
    result = ContinuationIterationSnapshot(
        producer_inputs=producer_inputs,
        last_validation_json=_validation_json(last_validation) if last_validation is not None else None,
        advisor_review_json=_dump_owned(_REVIEW, advisor_review),
        failed_turn_json=_dump_owned(_FAILURE, failed_turn) if failed_turn is not None else None,
        anti_anchor=anti_anchor,
        disclosure=disclosure,
    )
    iteration_values(result, env_ref_names=env_ref_names)
    return result


def encode_continuation_iteration(snapshot: ContinuationIterationSnapshot, *, env_ref_names: frozenset[str] = frozenset()) -> PrivateChunk:
    iteration_values(snapshot, env_ref_names=env_ref_names)
    return encode_private_chunk(
        codec="composer.iteration.v1",
        canonical=canonical_json(snapshot.model_dump(mode="json", by_alias=True)),
        env_ref_names=env_ref_names,
    )


def decode_continuation_iteration(chunk: PrivateChunk, *, env_ref_names: frozenset[str] = frozenset()) -> ContinuationIterationSnapshot:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.iteration.v1":
        raise AuditIntegrityError("iteration has another private codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        result = ContinuationIterationSnapshot.model_validate_json(chunk.canonical)
    except ValidationError as validation_error:
        raise AuditIntegrityError("iteration history violates its complete owned shape") from validation_error
    if encode_continuation_iteration(result, env_ref_names=env_ref_names).canonical != chunk.canonical:
        raise AuditIntegrityError("iteration history omitted or normalized a field")
    return result


def capture_admitted_schema_inputs(
    inputs: tuple[tuple[_AdmittedToolCall, AdmittedDiscoveryResult], ...],
    *,
    env_ref_names: frozenset[str],
) -> ContinuationProducerInputs:
    """Record actual successful selected schema producer inputs, without replaying prompt trust."""
    if type(inputs) is not tuple:
        raise TypeError("schema observations require their ordered actual cohort")
    retained: list[tuple[_AdmittedToolCall, CompositionState]] = []
    for item in inputs:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("schema observations require actual call/result pairs")
        call, admitted = item
        if (
            type(call) is not _AdmittedToolCall
            or type(call.function) is not _AdmittedToolFunction
            or type(admitted) is not AdmittedDiscoveryResult
            or admitted.contract is not PLUGIN_SCHEMA_RESPONSE_CONTRACT
            or type(admitted.response) is not AdmittedPluginSchemaResponse
        ):
            raise TypeError("schema observation lost its actual selected producer")
        response = admitted.response.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT)
        arguments, error = parse_json_strict(call.function.arguments)
        if (
            call.function.name != "get_plugin_schema"
            or admitted.result.success is not True
            or error is not None
            or type(arguments) is not dict
            or set(arguments) != {"plugin_type", "name"}
            or arguments["plugin_type"] != response.snapshot.plugin_type
            or arguments["name"] != response.snapshot.name
        ):
            raise AuditIntegrityError("schema observation differs from its actual successful selected call")
        require_retained_result_credentials("get_plugin_schema", admitted.to_dict(), env_ref_names=env_ref_names)
        retained.append((call, admitted.result.updated_state))
    return capture_continuation_producer_inputs(tuple(retained), env_ref_names=env_ref_names)
