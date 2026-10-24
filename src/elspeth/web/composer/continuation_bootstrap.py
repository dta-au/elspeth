from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ContinuationValue, LogicalRequestBinding
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, owned_json, verify_private_chunk
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.protocol import ComposerHistoryMessage, RetainedOperationContext
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.execution.completion_gates import CompletionGateFacts
from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import _composer_chat_history, _composer_conversation_messages
from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.titles import is_default_session_title

_RECORDS = TypeAdapter(tuple[ChatMessageRecord, ...])
_RECOVERY = TypeAdapter(GraphRecoveryContext)
_GATES = TypeAdapter(CompletionGateFacts)
_RETAINED = TypeAdapter(RetainedOperationContext)


class ContinuationBootstrapSnapshot(ContinuationValue):
    schema_version: Literal["composer.bootstrap.v1"] = "composer.bootstrap.v1"
    logical: LogicalRequestBinding
    actor_user_id: str
    request_json: str
    records_json: str
    original_last_sequence: int
    history_json: str
    has_partial_turn: bool
    initial_state_json: str
    recovery_context_json: str
    completion_gates_json: str | None
    retained_context_json: str | None
    original_session_title: str
    auto_title_eligible: bool
    initial_schema_disclosure: tuple[tuple[Literal["source", "transform", "sink"], str], ...]


@dataclass(frozen=True, slots=True)
class ContinuationBootstrapValues:
    snapshot: ContinuationBootstrapSnapshot
    request: SendMessageRequest | RecomposeRequest
    records: tuple[ChatMessageRecord, ...]
    history: tuple[ComposerHistoryMessage, ...]
    message: str
    initial_state: CompositionState
    recovery_context: GraphRecoveryContext
    completion_gates: CompletionGateFacts | None
    retained_context: RetainedOperationContext | None


def _canonical_records_json(records: tuple[ChatMessageRecord, ...]) -> str:
    if type(records) is not tuple or any(type(row) is not ChatMessageRecord for row in records):
        raise TypeError("bootstrap records require complete owned transcript rows")
    # The owned constructor freezes this JSON field to mappingproxy containers.
    # Validate its algebra before Pydantic can coerce a set to an array.
    tool_calls = [owned_json(row.tool_calls) for row in records]
    try:
        payload = _RECORDS.dump_python(records, mode="json", warnings="error", exclude={"__all__": {"tool_calls"}})
    except PydanticSerializationError as error:
        raise AuditIntegrityError("bootstrap owned records cannot be retained as JSON") from error
    for row, wire in zip(payload, tool_calls, strict=True):
        row["tool_calls"] = wire
    return canonical_json(owned_json(payload))


def _canonical_records(encoded: str) -> tuple[ChatMessageRecord, ...]:
    try:
        records = _RECORDS.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("bootstrap retained records violate their owned contract") from error
    if _canonical_records_json(records) != encoded:
        raise AuditIntegrityError("bootstrap owned records lost or changed a field")
    return records


def _canonical_adapter_json[T](adapter: TypeAdapter[T], value: T) -> str:
    try:
        payload = adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json)
    except PydanticSerializationError as error:
        raise AuditIntegrityError("bootstrap owned value cannot be retained as JSON") from error
    return canonical_json(owned_json(payload))


def _canonical_adapter[T](adapter: TypeAdapter[T], encoded: str) -> T:
    try:
        value = adapter.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("bootstrap retained value violates its owned contract") from error
    if _canonical_adapter_json(adapter, value) != encoded:
        raise AuditIntegrityError("bootstrap owned value lost or changed a field")
    return value


def bootstrap_values(snapshot: ContinuationBootstrapSnapshot) -> ContinuationBootstrapValues:
    if type(snapshot) is not ContinuationBootstrapSnapshot:
        raise TypeError("bootstrap decode requires the complete owned snapshot")
    logical = snapshot.logical
    request_type = SendMessageRequest if logical.kind == "compose_message" else RecomposeRequest
    request = request_type.model_validate_json(snapshot.request_json, strict=True)
    if (
        canonical_json(request.model_dump(mode="json")) != snapshot.request_json
        or composer_operation_v2_request_hash(session_id=UUID(logical.session_id), kind=logical.kind, request=request)
        != logical.request_hash
        or str(request.operation_id) != logical.operation_id
        or (str(request.state_id) if request.state_id is not None else None) != logical.base_state_id
    ):
        raise AuditIntegrityError("bootstrap changed its immutable operation request")
    records = _canonical_records(snapshot.records_json)
    if not records or any(str(row.session_id) != logical.session_id for row in records):
        raise AuditIntegrityError("bootstrap requires exact same-session sequenced transcript rows")
    sequence_values: list[int] = []
    for row in records:
        sequence_no = row.sequence_no
        if type(sequence_no) is not int:
            raise AuditIntegrityError("bootstrap requires exact same-session sequenced transcript rows")
        sequence_values.append(sequence_no)
    sequences = tuple(sequence_values)
    if sequences != tuple(sorted(set(sequences))) or sequences[-1] != snapshot.original_last_sequence:
        raise AuditIntegrityError("bootstrap transcript order or original cutoff changed")
    conversation = _composer_conversation_messages(records)
    human = next((row for row in reversed(conversation) if row.role == "user"), None)
    if (
        human is None
        or str(human.id) != logical.original_user_message_id
        or human.writer_principal not in ("route_user_message", "session_fork")
    ):
        raise AuditIntegrityError("bootstrap lost its exact original authenticated human request")
    index = conversation.index(human)
    partial = isinstance(request, RecomposeRequest) and any(row.role == "assistant" for row in conversation[index + 1 :])
    if isinstance(request, SendMessageRequest):
        if (
            conversation[-1].id != human.id
            or human.content != request.content
            or human.writer_principal != "route_user_message"
            or human.operation_id != UUID(request.operation_id)
        ):
            raise AuditIntegrityError("send bootstrap differs from its same-transaction ingress")
    else:
        if human.id != request.expected_user_message_id or any(
            row.role == "assistant" and not row.tool_calls for row in conversation[index + 1 :]
        ):
            raise AuditIntegrityError("recompose bootstrap has changed target or an existing final answer")
    history = tuple(_composer_chat_history(records if partial else tuple(row for row in records if row.id != human.id)))
    if snapshot.has_partial_turn is not partial or canonical_json(history) != snapshot.history_json:
        raise AuditIntegrityError("bootstrap changed original partial-turn, raw/display or control replay semantics")
    initial_payload, initial_error = parse_json_strict(snapshot.initial_state_json)
    if initial_error is not None or type(initial_payload) is not dict:
        raise AuditIntegrityError("bootstrap initial state is not a strict JSON object")
    initial_state = CompositionState.from_dict(initial_payload)
    if canonical_json(initial_state.to_dict()) != snapshot.initial_state_json:
        raise AuditIntegrityError("bootstrap initial state changed during owned decode")
    recovery = _canonical_adapter(_RECOVERY, snapshot.recovery_context_json)
    base_version = initial_state.version if logical.base_state_id is not None else 0
    if (
        recovery.malformed_head
        or recovery.turn_start_state_id != logical.base_state_id
        or recovery.turn_start_version != base_version
        or recovery.current_state_id != logical.base_state_id
        or recovery.current_version != base_version
    ):
        raise AuditIntegrityError("v2 bootstrap requires its actual valid admission base")
    gates = _canonical_adapter(_GATES, snapshot.completion_gates_json) if snapshot.completion_gates_json is not None else None
    retained = _canonical_adapter(_RETAINED, snapshot.retained_context_json) if snapshot.retained_context_json is not None else None
    if retained is not None:
        retained.validate_for(session_id=logical.session_id, actor_user_id=snapshot.actor_user_id)
    eligible = isinstance(request, SendMessageRequest) and len(records) == 1 and is_default_session_title(snapshot.original_session_title)
    if snapshot.auto_title_eligible is not eligible:
        raise AuditIntegrityError("bootstrap changed original optional title eligibility")
    return ContinuationBootstrapValues(snapshot, request, records, history, human.content, initial_state, recovery, gates, retained)


def capture_continuation_bootstrap(
    *,
    logical: LogicalRequestBinding,
    actor_user_id: str,
    request: SendMessageRequest | RecomposeRequest,
    records: tuple[ChatMessageRecord, ...],
    initial_state: CompositionState,
    recovery_context: GraphRecoveryContext,
    completion_gates: CompletionGateFacts | None,
    retained_context: RetainedOperationContext | None,
    session_title: str,
    initial_schema_disclosure: tuple[tuple[Literal["source", "transform", "sink"], str], ...],
) -> ContinuationBootstrapSnapshot:
    if type(request) not in (SendMessageRequest, RecomposeRequest) or type(initial_state) is not CompositionState:
        raise TypeError("bootstrap capture requires actual application ingress and state values")
    conversation = _composer_conversation_messages(records)
    human = next((row for row in reversed(conversation) if row.role == "user"), None)
    if human is None or not records or type(records[-1].sequence_no) is not int:
        raise AuditIntegrityError("bootstrap capture has no complete original ingress")
    partial = isinstance(request, RecomposeRequest) and any(
        row.role == "assistant" for row in conversation[conversation.index(human) + 1 :]
    )
    history = _composer_chat_history(records if partial else tuple(row for row in records if row.id != human.id))
    snapshot = ContinuationBootstrapSnapshot(
        logical=logical,
        actor_user_id=actor_user_id,
        request_json=canonical_json(request.model_dump(mode="json")),
        records_json=_canonical_records_json(records),
        original_last_sequence=records[-1].sequence_no,
        history_json=canonical_json(history),
        has_partial_turn=partial,
        initial_state_json=canonical_json(initial_state.to_dict()),
        recovery_context_json=_canonical_adapter_json(_RECOVERY, recovery_context),
        completion_gates_json=_canonical_adapter_json(_GATES, completion_gates) if completion_gates is not None else None,
        retained_context_json=_canonical_adapter_json(_RETAINED, retained_context) if retained_context is not None else None,
        original_session_title=session_title,
        auto_title_eligible=isinstance(request, SendMessageRequest) and len(records) == 1 and is_default_session_title(session_title),
        initial_schema_disclosure=initial_schema_disclosure,
    )
    bootstrap_values(snapshot)
    return snapshot


def encode_continuation_bootstrap(
    snapshot: ContinuationBootstrapSnapshot,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> PrivateChunk:
    values = bootstrap_values(snapshot)
    require_no_credential_material_in_state(
        values.initial_state, surface="retained Composer bootstrap initial state", env_ref_names=env_ref_names
    )
    return encode_private_chunk(
        codec="composer.bootstrap.v1", canonical=canonical_json(snapshot.model_dump(mode="json")), env_ref_names=env_ref_names
    )


def decode_continuation_bootstrap(chunk: PrivateChunk) -> ContinuationBootstrapSnapshot:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.bootstrap.v1":
        raise AuditIntegrityError("original Composer history requires its complete bootstrap codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        snapshot = ContinuationBootstrapSnapshot.model_validate_json(chunk.canonical)
        bootstrap_values(snapshot)
    except (ValueError, TypeError, ValidationError) as exc:
        raise AuditIntegrityError("retained bootstrap violates its complete owned shape") from exc
    if canonical_json(snapshot.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("bootstrap lost or changed a retained field")
    return snapshot
