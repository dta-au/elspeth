"""Actual selected current producers; saved recipes never contain prompt trust.

Synchronous construction/rebuild runs inside the existing required SQL worker.
Capture only records actual live producer outputs; it performs no provider work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardToolArgumentParts,
    detach_prompt_guard_evidence,
)
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolCall, _AdmittedToolFunction
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_bootstrap import (
    ContinuationBootstrapSnapshot,
    ContinuationBootstrapValues,
    _canonical_records_json,
    bootstrap_values,
    decode_continuation_bootstrap,
    encode_continuation_bootstrap,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection
from elspeth.web.composer.continuation_iteration import (
    ContinuationProducerInputs,
    capture_admitted_schema_inputs,
    capture_continuation_producer_inputs,
    decode_continuation_iteration,
    producer_input_states,
)
from elspeth.web.composer.continuation_message_builders import (
    build_service_initial_messages,
    recovery_instruction_message,
    reply_instruction_message,
    tool_result_message,
)
from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
from elspeth.web.composer.continuation_message_family_builders import (
    capture_failure_schema_recipe,
    capture_native_message_recipe,
    require_native_message_refresh,
)
from elspeth.web.composer.continuation_message_family_recipes import (
    AttachmentResultMessageRecipe,
    FailureSchemaMessageRecipe,
    NativeThinkingMessageRecipe,
    ToolCallArgumentsMessageRecipe,
)
from elspeth.web.composer.continuation_prompt_recipes import (
    ContinuationMessageRecipe,
    InitialMessageRecipe,
    PluginSchemaMessageRecipe,
    RecoveryInstructionRecipe,
    ReplyInstructionRecipe,
    ToolArgumentsRecipe,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.prompt_guard_rendering import json_argument_content_parts
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table, message_ingress_receipts_table
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.transcript_reads import non_private_message_predicate, validate_transcript_private_custody

if TYPE_CHECKING:
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot
    from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation_composition_initialization import CurrentCompositionInitializationSource

_GUARD_KEYS = frozenset(
    {PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_CONTENT_PARTS_KEY, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY, PROMPT_GUARD_THINKING_PARTS_KEY}
)
_NATIVE_KEYS = frozenset({"thinking_blocks", "reasoning_content", "reasoning_details"})


def _wire(message: Mapping[str, Any]) -> str:
    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    return encode_transcript_object_json(detach_prompt_guard_evidence([message]).messages[0])


def _retained_wire(text: str) -> dict[str, Any]:
    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    value, error = parse_json_strict(text)
    if error is not None or type(value) is not dict or _GUARD_KEYS.intersection(value) or encode_transcript_object_json(value) != text:
        raise AuditIntegrityError("retained message is not complete marker-free ordered wire")
    return value


def _argument_message(wire: Mapping[str, Any]) -> dict[str, Any]:
    if _NATIVE_KEYS.intersection(wire):
        raise AuditIntegrityError("native message requires its actual selected provider-source owner")
    calls = wire.get("tool_calls")
    if type(calls) is not list:
        raise AuditIntegrityError("argument producer requires a complete tool-call list")
    parts = []
    for index, call in enumerate(calls):
        if type(call) is not dict or type(call.get("function")) is not dict or type(call["function"].get("arguments")) is not str:
            raise AuditIntegrityError("argument producer lost complete raw function arguments")
        parts.append(PromptGuardToolArgumentParts(index, json_argument_content_parts(call["function"]["arguments"])))
    result = dict(wire)
    result[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = tuple(parts)
    return result


@dataclass(frozen=True, slots=True)
class _CurrentProducerInputs:
    bootstrap: ContinuationBootstrapSnapshot
    values: ContinuationBootstrapValues
    producer_inputs: ContinuationProducerInputs
    evidence_messages_json: str
    evidence: TurnEvidenceSnapshot


def require_original_bootstrap_transcript_on_connection(conn: Connection, bootstrap: ContinuationBootstrapSnapshot) -> None:
    """Exact original prefix/private/ingress proof; current authority belongs to the caller."""
    validate_transcript_private_custody(conn, bootstrap.logical.session_id)
    rows = conn.execute(
        select(chat_messages_table, message_ingress_receipts_table.c.operation_id)
        .select_from(
            chat_messages_table.outerjoin(
                message_ingress_receipts_table,
                (message_ingress_receipts_table.c.user_message_id == chat_messages_table.c.id)
                & (message_ingress_receipts_table.c.session_id == chat_messages_table.c.session_id),
            )
        )
        .where(
            chat_messages_table.c.session_id == bootstrap.logical.session_id,
            chat_messages_table.c.sequence_no <= bootstrap.original_last_sequence,
            non_private_message_predicate(conn.dialect.name),
        )
        .order_by(chat_messages_table.c.sequence_no)
    ).all()
    if bootstrap.logical.kind == "compose_message":
        ingress = conn.execute(
            select(message_ingress_receipts_table).where(
                message_ingress_receipts_table.c.session_id == bootstrap.logical.session_id,
                message_ingress_receipts_table.c.operation_id == bootstrap.logical.operation_id,
            )
        ).one_or_none()
        if (
            ingress is None
            or ingress.user_message_id != bootstrap.logical.original_user_message_id
            or ingress.requested_state_id != bootstrap.logical.base_state_id
        ):
            raise AuditIntegrityError("message producer lost its actual immutable same-session ingress receipt")
        # The actual add_message_with_transcript producer attaches only this
        # ingress operation to its last user row, leaving prior rows unchanged.
        actual = tuple(
            chat_message_record_from_row(row, operation_id=ingress.operation_id if row.id == ingress.user_message_id else None)
            for row in rows
        )
    else:
        # Actual get_messages recompose history includes each original ingress
        # receipt's operation id, never the current recompose operation id.
        actual = tuple(chat_message_record_from_row(row, operation_id=row.operation_id) for row in rows)
    if _canonical_records_json(actual) != bootstrap.records_json:
        raise AuditIntegrityError("message inputs changed their complete original ordered transcript prefix")


def _read_current_inputs(
    conn: Connection,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> _CurrentProducerInputs:
    # This real reader proves ordinary current claim/lease/CAS, private custody,
    # original actor/request/base and transcript/evidence/recorder links.
    evidence = read_turn_evidence_on_connection(conn, inventory.running, selected=selected, inventory=inventory)
    return _inputs_from_current_evidence_on_connection(conn, selected=selected, inventory=inventory, evidence=evidence)


def _inputs_from_current_evidence_on_connection(
    conn: Connection, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory, evidence: TurnEvidenceSnapshot
) -> _CurrentProducerInputs:
    """Shared DATA projection; its caller owns the actual SQL evidence read."""
    from elspeth.web.composer.continuation_message_shelf import encode_message_shelf

    frames = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(frames) != 1:
        raise AuditIntegrityError("message inputs require one selected Compose frame")
    frame = frames[0]

    def selected_chunk(ref: PrivateChunkRef) -> PrivateChunk:
        matches = tuple(chunk for chunk in selected.chunks if chunk.reference == ref)
        if len(matches) != 1:
            raise AuditIntegrityError("message inputs lost their exact selected private chunk")
        chunk = matches[0]
        raw = read_chunk(
            conn,
            session_id=selected.stack.logical.session_id,
            operation_id=selected.stack.logical.operation_id,
            key=f"value:{ref.codec}:{ref.digest}",
        )
        if raw != chunk.canonical.encode("utf-8"):
            raise AuditIntegrityError("message input storage differs from the complete selected bytes")
        verify_private_chunk(ref, chunk.canonical)
        return chunk

    bootstrap = decode_continuation_bootstrap(selected_chunk(frame.bootstrap))
    values = bootstrap_values(bootstrap)
    encode_continuation_bootstrap(bootstrap, env_ref_names=inventory.names)
    if (
        bootstrap.logical != selected.stack.logical
        or bootstrap.actor_user_id != evidence.actor_user_id
        or values.message != evidence.transcript.request
        or bootstrap.initial_state_json != evidence.effects.initial_state_json
    ):
        raise AuditIntegrityError("message inputs changed original actor/request/initial state")
    require_original_bootstrap_transcript_on_connection(conn, bootstrap)
    iteration = decode_continuation_iteration(selected_chunk(frame.iteration), env_ref_names=inventory.names)
    return _CurrentProducerInputs(
        bootstrap, values, iteration.producer_inputs, encode_message_shelf(evidence.transcript).canonical, evidence
    )


@dataclass(frozen=True, slots=True)
class _ReadyProducerInputs:
    bootstrap: ContinuationBootstrapSnapshot
    values: ContinuationBootstrapValues
    producer_inputs: ContinuationProducerInputs
    source: CurrentCompositionInitializationSource


@dataclass(slots=True)
class CurrentContinuationMessageProducers:
    service: ComposerServiceImpl = field(repr=False)
    authority: ComposerAsyncOperationAuthority = field(repr=False)
    selected: SelectedContinuationState = field(repr=False)
    inventory: CurrentContinuationEnvInventory = field(repr=False)
    inputs: _CurrentProducerInputs | _ReadyProducerInputs = field(repr=False)
    _produced: list[tuple[Mapping[str, Any], ContinuationMessageRecipe, str]] = field(default_factory=list, init=False, repr=False)
    _schema_inputs: list[tuple[_AdmittedToolCall, CompositionState]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        from elspeth.web.composer.service import ComposerServiceImpl
        from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

        if (
            type(self.service) is not ComposerServiceImpl
            or type(self.authority) is not ComposerAsyncOperationAuthority
            or type(self.selected) is not SelectedContinuationState
            or type(self.inventory) is not CurrentContinuationEnvInventory
            or type(self.inputs) not in (_CurrentProducerInputs, _ReadyProducerInputs)
        ):
            raise AuditIntegrityError("message producer requires its actual current owners")
        sessions = self.service._require_sessions_service()
        if type(sessions) is not SessionServiceImpl or sessions._engine is not self.authority._engine:
            raise AuditIntegrityError("message producer crossed actual session stores")
        states = producer_input_states(self.inputs.producer_inputs, env_ref_names=self.inventory.names)
        for entry in self.inputs.producer_inputs.schema_inputs:
            self._schema_inputs.append(
                (
                    _AdmittedToolCall(entry.tool_call_id, _AdmittedToolFunction("get_plugin_schema", entry.raw_arguments)),
                    states[entry.state_index],
                )
            )

    def _require_current(self) -> _CurrentProducerInputs:
        if type(self.inputs) is not _CurrentProducerInputs:
            raise AuditIntegrityError("ready message producer has no existing Compose/family source")
        with locked_session_transaction(self.authority._engine, str(self.inventory.running.claim.session_id)) as conn:
            actual = _read_current_inputs(conn, selected=self.selected, inventory=self.inventory)
        if (
            actual.bootstrap != self.inputs.bootstrap
            or actual.producer_inputs != self.inputs.producer_inputs
            or actual.evidence_messages_json != self.inputs.evidence_messages_json
        ):
            raise AuditIntegrityError("message producer inputs differ from their current selected owner")
        return actual

    def _require_initial_current(self) -> None:
        inputs = self.inputs
        if type(inputs) is _CurrentProducerInputs:
            self._require_current()
            return
        if type(inputs) is not _ReadyProducerInputs:
            raise AuditIntegrityError("message producer requires its actual current owners")
        from elspeth.web.sessions.continuation_composition_initialization import read_composition_initialization_source_on_connection

        with locked_session_transaction(self.authority._engine, str(self.inventory.running.claim.session_id)) as conn:
            actual = read_composition_initialization_source_on_connection(
                conn, self.service, self.inventory.running, selected=self.selected, inventory=self.inventory
            )
        if actual.initialized or not actual.same_original_source(inputs.source):
            raise AuditIntegrityError("ready initial producer changed its actual complete current source")

    def _require_compose_family(self) -> None:
        if type(self.inputs) is not _CurrentProducerInputs:
            raise AuditIntegrityError("ready message producer has no actual noninitial family source")

    def require_context(self, *, session_id: str | None, context: object, actor_user_id: str | None) -> None:
        if (
            session_id != self.inputs.bootstrap.logical.session_id
            or context is not self.inventory.running.session_operation_context
            or actor_user_id != self.inputs.bootstrap.actor_user_id
        ):
            raise AuditIntegrityError("live message producer crossed its actual current context")

    def _register(self, message: dict[str, Any], recipe: ContinuationMessageRecipe) -> dict[str, Any]:
        if type(self.inputs) is _ReadyProducerInputs and type(recipe) is not InitialMessageRecipe:
            raise AuditIntegrityError("ready message producer admits only its actual initial-message family")
        if any(existing is message for existing, _, _ in self._produced):
            raise AuditIntegrityError("message producer registered the same actual object twice")
        self._produced.append((message, recipe, _wire(message)))
        return message

    def _initial(self) -> list[dict[str, Any]]:
        values = self.inputs.values
        snapshot, catalog = self.service._policy_context.build(self.inputs.bootstrap.actor_user_id)
        for kind, name in self.inputs.bootstrap.initial_schema_disclosure:
            catalog.get_schema(kind, name)
        return build_service_initial_messages(
            self.service,
            chat_history=list(values.history),
            state=values.initial_state,
            user_message=values.message,
            session_id=self.inputs.bootstrap.logical.session_id,
            user_id=self.inputs.bootstrap.actor_user_id,
            plugin_snapshot=snapshot,
            policy_catalog=catalog,
            session_operation_context=self.inventory.running.session_operation_context,
            env_ref_names=self.inventory.names,
            schemas_loaded=frozenset(self.inputs.bootstrap.initial_schema_disclosure),
        )

    def initial_messages(self) -> list[dict[str, Any]]:
        self._require_initial_current()
        messages = self._initial()
        self._require_initial_current()
        return [self._register(message, InitialMessageRecipe(index=index)) for index, message in enumerate(messages)]

    def recovery_instruction(self) -> dict[str, Any]:
        return self._register(recovery_instruction_message(), RecoveryInstructionRecipe())

    def reply_instruction(self) -> dict[str, Any]:
        return self._register(reply_instruction_message(), ReplyInstructionRecipe())

    def tool_arguments(self, message: Mapping[str, Any]) -> dict[str, Any]:
        return self._register(_argument_message(_retained_wire(_wire(message))), ToolArgumentsRecipe())

    def register_tool_call_message(
        self,
        *,
        response: PrivateChunk,
        completion: _AdmittedLLMCompletion,
        message: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        """Retain the same actual B2 object, then refresh its one admitted call."""
        self._require_compose_family()
        from elspeth.web.composer.continuation_tool_argument_messages import (
            capture_tool_call_message_recipe,
            require_tool_call_message_refresh,
        )

        recipe = capture_tool_call_message_recipe(response=response, completion=completion, message=message)
        existing = tuple((index, previous, wire) for index, (actual, previous, wire) in enumerate(self._produced) if actual is message)
        if not existing:
            if tool_call_id is not None:
                raise AuditIntegrityError("argument refresh lost its original same-object registration")
            require_tool_call_message_refresh(
                previous=None, previous_wire=None, recipe=recipe, completion=completion, message=message, tool_call_id=None
            )
            return self._register(message, recipe)
        if len(existing) != 1:
            raise AuditIntegrityError("argument refresh duplicated its original registration")
        index, previous, wire = existing[0]
        require_tool_call_message_refresh(
            previous=previous, previous_wire=wire, recipe=recipe, completion=completion, message=message, tool_call_id=tool_call_id
        )
        self._produced[index] = (message, recipe, _wire(message))
        return message

    def register_schema_message(
        self, *, call: _AdmittedToolCall, admitted: AdmittedDiscoveryResult, message: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_compose_family()
        capture_admitted_schema_inputs(((call, admitted),), env_ref_names=self.inventory.names)
        if _wire(message) != _wire(
            tool_result_message(call_id=call.id, result=admitted, serialized=serialize_admitted_discovery_result(admitted))
        ):
            raise AuditIntegrityError("schema registration changed the actual complete result message")
        index = len(self._schema_inputs)
        self._schema_inputs.append((call, admitted.result.updated_state))
        return self._register(message, PluginSchemaMessageRecipe(input_index=index))

    def snapshot_inputs(self) -> ContinuationProducerInputs:
        return capture_continuation_producer_inputs(tuple(self._schema_inputs), env_ref_names=self.inventory.names)

    def register_native_message(
        self,
        *,
        response: PrivateChunk,
        completion: _AdmittedLLMCompletion,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_compose_family()
        recipe = capture_native_message_recipe(response=response, completion=completion, message=message)
        existing = tuple((index, previous, wire) for index, (actual, previous, wire) in enumerate(self._produced) if actual is message)
        if not existing:
            return self._register(message, recipe)
        if len(existing) != 1:
            raise AuditIntegrityError("native message refresh replaced its actual registered family")
        index, previous, wire = existing[0]
        if type(previous) is not NativeThinkingMessageRecipe:
            raise AuditIntegrityError("native message refresh replaced its actual registered family")
        require_native_message_refresh(previous=previous, previous_wire=wire, recipe=recipe, message=message)
        self._produced[index] = (message, recipe, _wire(message))
        return message

    def register_failure_schema_message(
        self,
        *,
        response: PrivateChunk,
        call: _AdmittedToolCall,
        result: ToolResult,
        catalog: PolicyCatalogView,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_compose_family()
        recipe = capture_failure_schema_recipe(response=response, call=call, result=result, catalog=catalog, message=message)
        return self._register(message, recipe)

    def register_attachment_message(
        self, *, response: PrivateChunk, call: _AdmittedToolCall, result: ToolResult, message: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_compose_family()
        from elspeth.web.composer.continuation_message_family_builders import capture_attachment_result_recipe

        recipe = capture_attachment_result_recipe(response=response, call=call, result=result, message=message)
        return self._register(message, recipe)

    def capture_message(self, message: Mapping[str, Any]) -> tuple[str, ContinuationMessageRecipe | None]:
        encoded = _wire(message)
        entries = tuple((recipe, original) for actual, recipe, original in self._produced if actual is message)
        if not entries:
            if _GUARD_KEYS.intersection(message) or _NATIVE_KEYS.intersection(_retained_wire(encoded)):
                raise AuditIntegrityError("guarded/native message has no actual registered current producer")
            return encoded, None
        if len(entries) != 1 or entries[0][1] != encoded:
            raise AuditIntegrityError("registered message changed its complete original wire")
        if type(entries[0][0]) is ToolCallArgumentsMessageRecipe:
            from elspeth.web.composer.continuation_tool_argument_messages import require_ordinary_argument_parts

            require_ordinary_argument_parts(message, entries[0][0].argument_indexes)
        return encoded, entries[0][0]

    def _schema_message(self, index: int) -> dict[str, Any]:
        if type(self.inputs) is not _CurrentProducerInputs:
            raise AuditIntegrityError("ready message producer has no actual schema-call source")
        if type(index) is not int or not 0 <= index < len(self._schema_inputs):
            raise AuditIntegrityError("schema recipe lost its actual input position")
        call, state = self._schema_inputs[index]
        arguments, error = parse_json_strict(call.function.arguments)
        if error is not None or type(arguments) is not dict:
            raise AuditIntegrityError("schema recipe lost its complete raw arguments")
        snapshot, catalog = self.service._policy_context.build(self.inputs.bootstrap.actor_user_id)
        result = execute_tool(
            "get_plugin_schema",
            arguments,
            state,
            catalog,
            plugin_snapshot=snapshot,
            user_id=self.inputs.bootstrap.actor_user_id,
            validate_arguments=True,
        )
        admitted = admit_discovery_result("get_plugin_schema", result)
        capture_admitted_schema_inputs(((call, admitted),), env_ref_names=self.inventory.names)
        return tool_result_message(call_id=call.id, result=admitted, serialized=serialize_admitted_discovery_result(admitted))

    def restore_shelf(self, snapshot: MessageShelfSnapshot) -> list[dict[str, Any]]:
        inputs = self._require_current()
        restored = self._rebuild_shelf(snapshot, inputs=inputs)
        self._require_current()
        return restored

    def _rebuild_shelf(self, snapshot: MessageShelfSnapshot, *, inputs: _CurrentProducerInputs) -> list[dict[str, Any]]:
        """Actual assembly only; current read brackets remain caller-owned."""
        from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, encode_message_shelf

        if type(inputs) is not _CurrentProducerInputs or inputs != self.inputs:
            raise AuditIntegrityError("shelf assembly changed its actual producer inputs")
        if type(snapshot) is not MessageShelfSnapshot or encode_message_shelf(snapshot).canonical != inputs.evidence_messages_json:
            raise AuditIntegrityError("message reconstruction changed the complete selected transcript shelf")
        messages_json = snapshot.messages_json
        producer_recipes = snapshot.producer_recipes or (None,) * len(messages_json)
        if len(messages_json) != len(producer_recipes):
            raise AuditIntegrityError("message recipes lost complete shelf positions")
        family_positions: list[int] = []
        family_requests: list[
            tuple[
                NativeThinkingMessageRecipe | ToolCallArgumentsMessageRecipe | FailureSchemaMessageRecipe | AttachmentResultMessageRecipe,
                str,
            ]
        ] = []
        for index, (text, recipe) in enumerate(zip(messages_json, producer_recipes, strict=True)):
            if (
                type(recipe) is NativeThinkingMessageRecipe
                or type(recipe) is ToolCallArgumentsMessageRecipe
                or type(recipe) is FailureSchemaMessageRecipe
                or type(recipe) is AttachmentResultMessageRecipe
            ):
                family_positions.append(index)
                family_requests.append((recipe, text))
        # The cohort transaction ends before initial-message content-scope CAS
        # or schema policy callbacks can open their own current SQL transaction.
        family_messages = CurrentContinuationMessageFamilies(self.service, self.authority, self.selected, self.inventory).restore_messages(
            tuple(family_requests)
        )
        by_position = dict(zip(family_positions, family_messages, strict=True))
        restored: list[dict[str, Any]] = []
        initial: list[dict[str, Any]] | None = None
        for index, (text, recipe) in enumerate(zip(messages_json, producer_recipes, strict=True)):
            wire = _retained_wire(text)
            if recipe is None:
                if _NATIVE_KEYS.intersection(wire):
                    raise AuditIntegrityError("native message lacks its selected provider-source owner")
                restored.append(wire)
                continue
            if type(recipe) is InitialMessageRecipe:
                if initial is None:
                    initial = self._initial()
                if recipe.index >= len(initial):
                    raise AuditIntegrityError("initial recipe changed its complete position")
                current = dict(initial[recipe.index])
            elif type(recipe) is RecoveryInstructionRecipe:
                current = recovery_instruction_message()
            elif type(recipe) is ReplyInstructionRecipe:
                current = reply_instruction_message()
            elif type(recipe) is ToolArgumentsRecipe:
                current = _argument_message(wire)
            elif type(recipe) is PluginSchemaMessageRecipe:
                current = self._schema_message(recipe.input_index)
            elif (
                type(recipe) is ToolCallArgumentsMessageRecipe
                or type(recipe) is NativeThinkingMessageRecipe
                or type(recipe) is AttachmentResultMessageRecipe
                or type(recipe) is FailureSchemaMessageRecipe
            ):
                current = by_position[index]
            else:
                raise AuditIntegrityError("message recipe is outside the actual supported producer family")
            if _wire(current) != text:
                raise AuditIntegrityError("current producer differs from complete retained ordered wire")
            restored.append(self._register(current, recipe))
        return restored


@dataclass(frozen=True, slots=True)
class CurrentMessageProduction:
    producers: CurrentContinuationMessageProducers
    messages: tuple[dict[str, Any], ...] | None


def _produce_current_messages(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    retained_shelf: MessageShelfSnapshot | None,
    initial: bool,
) -> CurrentMessageProduction:
    from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, encode_message_shelf
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if type(service) is not ComposerServiceImpl or type(authority) is not ComposerAsyncOperationAuthority:
        raise AuditIntegrityError("message production requires actual service and SQL authority owners")
    if type(selected) is not SelectedContinuationState or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("message production requires actual current selection and inventory")
    with locked_session_transaction(authority._engine, str(inventory.running.claim.session_id)) as conn:
        inputs = _read_current_inputs(conn, selected=selected, inventory=inventory)
    owner = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs)
    if retained_shelf is not None:
        if (
            type(retained_shelf) is not MessageShelfSnapshot
            or encode_message_shelf(retained_shelf).canonical != inputs.evidence_messages_json
            or initial
        ):
            raise AuditIntegrityError("message production changed the selected evidence shelf")
        messages = owner.restore_shelf(retained_shelf)
        return CurrentMessageProduction(owner, tuple(messages[index] for index in retained_shelf.current_indexes))
    return CurrentMessageProduction(owner, tuple(owner.initial_messages()) if initial else None)


async def load_current_message_producers(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    retained_shelf: MessageShelfSnapshot | None = None,
    initial: bool = False,
) -> CurrentMessageProduction:
    if type(required_work) is not RequiredWorkBinding or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("message production lost its required-work/current inventory")
    running = required_work.running
    if type(running) is not ComposerOperationRunning or inventory.running is not running:
        raise AuditIntegrityError("message production changed its current running owner")
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("message production changed its required-work claim")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            _produce_current_messages,
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            retained_shelf=retained_shelf,
            initial=initial,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    actual: CurrentMessageProduction | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not CurrentMessageProduction:
                raise AuditIntegrityError("message projection lost its actual produced owner")
            actual = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        errors.append(AuditIntegrityError("message production returned an unknown SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Continuation current message outcomes", errors)
    if actual is None:
        raise AuditIntegrityError("message production lost its result")
    return actual
