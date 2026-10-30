"""Actual selected current producers; saved recipes never contain prompt trust.

Synchronous construction/rebuild runs inside the existing required SQL worker.
Capture only records actual live producer outputs; it performs no provider work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, select

from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateToolAuthoring
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_PROVENANCE_KEYS,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PromptGuardContentPart,
    PromptGuardToolArgumentParts,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
)
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef, ToolBatchFrame
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
from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence, require_admitted_turn_evidence
from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection
from elspeth.web.composer.continuation_iteration import (
    ContinuationProducerInputs,
    capture_admitted_schema_inputs,
    capture_continuation_producer_inputs,
    decode_continuation_iteration,
    producer_input_states,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
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
    capture_tool_feedback_recipe,
    capture_tool_result_recipe,
    require_feedback_invocation,
    require_native_message_refresh,
)
from elspeth.web.composer.continuation_message_family_recipes import (
    AttachmentResultMessageRecipe,
    FailureSchemaMessageRecipe,
    NativeThinkingMessageRecipe,
    ToolCallArgumentsMessageRecipe,
    ToolFeedbackMessageRecipe,
    ToolResultMessageRecipe,
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
    from elspeth.web.composer.audit import BufferingRecorder, DispatchAudit
    from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot
    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
    from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot
    from elspeth.web.composer.continuation_retention import OrderedObjectJSON
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.tool_batch import ContinuationBatchState, ContinuationCallState
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation_composition_initialization import CurrentCompositionInitializationSource

_NATIVE_KEYS = frozenset({"thinking_blocks", "reasoning_content", "reasoning_details"})


def _wire(message: Mapping[str, Any]) -> str:
    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    return encode_transcript_object_json(detach_prompt_guard_evidence([message]).messages[0])


def _retained_wire(text: str) -> dict[str, Any]:
    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    value, error = parse_json_strict(text)
    if (
        error is not None
        or type(value) is not dict
        or PROMPT_GUARD_PROVENANCE_KEYS.intersection(value)
        or encode_transcript_object_json(value) != text
    ):
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
    admitted_evidence: AdmittedTurnEvidence = field(compare=False, repr=False)
    evidence: TurnEvidenceSnapshot = field(init=False)

    def __post_init__(self) -> None:
        if type(self.admitted_evidence) is not AdmittedTurnEvidence:
            raise TypeError("message inputs require their actual source-admitted evidence")
        object.__setattr__(self, "evidence", self.admitted_evidence.snapshot)


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
    finish_read: ContinuationFinishRead | None = None,
) -> _CurrentProducerInputs:
    # This real reader proves ordinary current claim/lease/CAS, private custody,
    # original actor/request/base and transcript/evidence/recorder links.
    if finish_read is None:
        evidence = read_turn_evidence_on_connection(conn, inventory.running, selected=selected, inventory=inventory)
    else:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        if type(finish_read) is not ContinuationFinishRead:
            raise AuditIntegrityError("message read received a foreign owed-finish facet")
        finish_read.require_selection(selected, inventory)
        evidence = finish_read.read_evidence(conn)
    return _inputs_from_current_evidence_on_connection(conn, selected=selected, inventory=inventory, evidence=evidence)


def _inputs_from_current_evidence_on_connection(
    conn: Connection, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory, evidence: AdmittedTurnEvidence
) -> _CurrentProducerInputs:
    """Shared DATA projection; its caller owns the actual SQL evidence read."""
    from elspeth.web.composer.continuation_message_shelf import encode_message_shelf

    admitted = require_admitted_turn_evidence(evidence, env_ref_names=inventory.names)
    snapshot = admitted.snapshot
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
        or bootstrap.actor_user_id != snapshot.actor_user_id
        or values.message != snapshot.transcript.request
        or bootstrap.initial_state_json != snapshot.effects.initial_state_json
    ):
        raise AuditIntegrityError("message inputs changed original actor/request/initial state")
    require_original_bootstrap_transcript_on_connection(conn, bootstrap)
    iteration = decode_continuation_iteration(selected_chunk(frame.iteration), env_ref_names=inventory.names)
    from elspeth.web.composer.continuation_message_families import (
        _admit_selected_tool_feedback_recipes_on_connection,
        _admit_selected_tool_result_recipes_on_connection,
    )

    _admit_selected_tool_result_recipes_on_connection(conn, inventory.running, selected=selected, admitted=admitted, names=inventory.names)
    _admit_selected_tool_feedback_recipes_on_connection(
        conn, inventory.running, selected=selected, admitted=admitted, names=inventory.names
    )
    return _CurrentProducerInputs(
        bootstrap, values, iteration.producer_inputs, encode_message_shelf(snapshot.transcript).canonical, admitted
    )


@dataclass(frozen=True, slots=True)
class _ReadyProducerInputs:
    bootstrap: ContinuationBootstrapSnapshot
    values: ContinuationBootstrapValues
    producer_inputs: ContinuationProducerInputs
    source: CurrentCompositionInitializationSource


@dataclass(frozen=True, slots=True)
class _FeedbackMessageProvenance:
    """Original alias/source facts, without allocating an enclosing wire."""

    message: dict[str, Any]
    recipe: ToolFeedbackMessageRecipe
    keys: tuple[str, ...]
    role: str
    call_id: str
    body: str
    selector: tuple[str, str, int, int, str, str]
    call: _AdmittedToolCall
    invocation: ComposerToolInvocation
    authoring: PrivateToolAuthoring
    outcome: _ToolOutcome | None


@dataclass(slots=True)
class CurrentContinuationMessageProducers:
    service: ComposerServiceImpl = field(repr=False)
    authority: ComposerAsyncOperationAuthority = field(repr=False)
    selected: SelectedContinuationState = field(repr=False)
    inventory: CurrentContinuationEnvInventory = field(repr=False)
    inputs: _CurrentProducerInputs | _ReadyProducerInputs = field(repr=False)
    finish_read: ContinuationFinishRead | None = field(default=None, repr=False)
    _produced: list[tuple[Mapping[str, Any], ContinuationMessageRecipe, str]] = field(default_factory=list, init=False, repr=False)
    _schema_inputs: list[tuple[_AdmittedToolCall, CompositionState]] = field(default_factory=list, init=False, repr=False)
    _feedback_produced: list[_FeedbackMessageProvenance] = field(default_factory=list, init=False, repr=False)
    _tool_result_provenance: list[
        tuple[
            Mapping[str, Any],
            str,
            tuple[PromptGuardContentPart, ...],
            tuple[tuple[PromptGuardContentPart, str, PromptGuardTrust | None, str | None], ...],
        ]
    ] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
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
        if self.finish_read is not None:
            if type(self.finish_read) is not ContinuationFinishRead:
                raise AuditIntegrityError("message producer received a foreign owed-finish facet")
            self.finish_read.require_selection(self.selected, self.inventory)
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

    def advance_current(self, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory) -> None:
        """Rebind one live producer after its real same-owner SQL transition.

        Fresh source admission precedes installation. Existing SDK message and
        schema objects stay owned here; this read never dispatches or restores
        replacements for those objects, and cannot cross a physical adoption.
        """
        if (
            type(selected) is not SelectedContinuationState
            or type(inventory) is not CurrentContinuationEnvInventory
            or type(self.inputs) is not _CurrentProducerInputs
            or inventory.running is not self.inventory.running
            or inventory.running.session_operation_context is not self.inventory.running.session_operation_context
            or inventory.names != self.inventory.names
        ):
            raise AuditIntegrityError("live message advance changed its actual running/context/inventory owners")
        before, after = self.selected.stack, selected.stack
        if (
            after.logical != before.logical
            or after.cursor <= before.cursor
            or after.admission_policy_json != before.admission_policy_json
            or after.policy_hash != before.policy_hash
            or after.mutation_ceiling != before.mutation_ceiling
            or after.provider_cost_policy != before.provider_cost_policy
        ):
            raise AuditIntegrityError("live message advance changed its request, retained policy or predecessor order")
        selected.__post_init__()
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        finish_read = None if self.finish_read is None else ContinuationFinishRead(inventory.running, selected, inventory)
        with locked_session_transaction(self.authority._engine, str(inventory.running.claim.session_id)) as conn:
            from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn

            try:
                actual = _read_current_inputs(conn, selected=selected, inventory=inventory, finish_read=finish_read)
            except ComposerOperationCancelledDuringTurn:
                finish_read = ContinuationFinishRead(inventory.running, selected, inventory)
                actual = _read_current_inputs(conn, selected=selected, inventory=inventory, finish_read=finish_read)
            if (
                actual.bootstrap != self.inputs.bootstrap
                or actual.bootstrap.actor_user_id != self.inputs.bootstrap.actor_user_id
                or actual.producer_inputs != capture_continuation_producer_inputs(tuple(self._schema_inputs), env_ref_names=inventory.names)
            ):
                raise AuditIntegrityError("live message advance changed its original actor or actual schema history")
        # No callbacks, allocation of SDK objects or await between these writes.
        # A failed SQL read/transaction leaves every old binding intact.
        self.selected, self.inventory, self.inputs, self.finish_read = selected, inventory, actual, finish_read

    def _require_current(self) -> _CurrentProducerInputs:
        if type(self.inputs) is not _CurrentProducerInputs:
            raise AuditIntegrityError("ready message producer has no existing Compose/family source")
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
        from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn

        finish_read = self.finish_read
        with locked_session_transaction(self.authority._engine, str(self.inventory.running.claim.session_id)) as conn:
            try:
                actual = _read_current_inputs(conn, selected=self.selected, inventory=self.inventory, finish_read=finish_read)
            except ComposerOperationCancelledDuringTurn:
                finish_read = ContinuationFinishRead(self.inventory.running, self.selected, self.inventory)
                actual = _read_current_inputs(conn, selected=self.selected, inventory=self.inventory, finish_read=finish_read)
        if (
            actual.bootstrap != self.inputs.bootstrap
            or actual.producer_inputs != self.inputs.producer_inputs
            or actual.evidence_messages_json != self.inputs.evidence_messages_json
        ):
            raise AuditIntegrityError("message producer inputs differ from their current selected owner")
        self.finish_read = finish_read
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
        if any(existing is message for existing, _, _ in self._produced) or any(
            item.message is message for item in self._feedback_produced
        ):
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

    def register_tool_result_message(
        self,
        *,
        completion: _AdmittedLLMCompletion,
        tool_index: int,
        call: _AdmittedToolCall,
        result: ToolResult | AdmittedDiscoveryResult,
        serialized: str,
        message: dict[str, Any],
        catalog: PolicyCatalogView | None = None,
    ) -> dict[str, Any]:
        """Retain private provenance at the actual same-object publication seam.

        The driver's issued-call owner precedes execution. This entry supplies
        capture DATA for its atomic completion; it is not durable admission or
        a reason to read an uncommitted result. Those SQL proofs remain owned
        by the completion writer and every later current-source reader.
        """
        self._require_compose_family()
        selected = self.selected
        selected.__post_init__()
        compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
        top = selected.stack.frames[-1]
        if (
            len(compose) != 1
            or type(top) is not ToolBatchFrame
            or top.phase != "effect_reconciling"
            or top.accumulator.codec != "composer.tool-batch.v1"
            or compose[0].pending_response != top.response
            or top.next_tool_index != tool_index
            or compose[0].slice_sequence != self.inventory.running.claim.segment_sequence
        ):
            raise AuditIntegrityError("tool result producer lost its driver's actual issued selection")
        actual_result = result.result if type(result) is AdmittedDiscoveryResult else result
        if type(actual_result) is not ToolResult:
            raise AuditIntegrityError("tool result producer lost its actual nominal result")
        if actual_result.plugin_schemas is not None:
            if catalog is None:
                raise AuditIntegrityError("schema-augmented result requires its actual current policy catalog")
            found = tuple(chunk for chunk in selected.chunks if chunk.reference == top.response)
            if len(found) != 1:
                raise AuditIntegrityError("schema-augmented result lost its actual retained paid source")
            return self.register_failure_schema_message(
                response=found[0], call=call, result=actual_result, catalog=catalog, message=message
            )
        recipe = capture_tool_result_recipe(
            response=top.response,
            completion=completion,
            tool_index=tool_index,
            call=call,
            result=result,
            serialized=serialized,
            message=message,
        )
        registered = self._register(message, recipe)
        self._remember_tool_result_provenance(registered)
        return registered

    def _remember_tool_result_provenance(self, message: dict[str, Any]) -> None:
        """Keep alias/immutable-part facts from this actual producer output."""
        if any(actual is message for actual, *_rest in self._tool_result_provenance):
            raise AuditIntegrityError("tool result producer duplicated its live provenance")
        content, parts = message["content"], message[PROMPT_GUARD_CONTENT_PARTS_KEY]
        if type(content) is not str or type(parts) is not tuple or any(type(part) is not PromptGuardContentPart for part in parts):
            raise AuditIntegrityError("tool result producer lost its nominal original content partition")
        facts = tuple((part, part.text, part.trust, part.inspection_text) for part in parts)
        self._tool_result_provenance.append((message, content, parts, facts))

    def register_tool_feedback_message(
        self,
        *,
        publication: ContinuationToolMessagePublication,
        serialized: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Private same-object provenance, before the atomic completion writer.

        Session-aware publication precedes its outer outcome. It binds the
        actual recorded invocation here; every later admission must prove the
        committed outer outcome and issue/history independently.
        """
        if type(publication) is not ContinuationToolMessagePublication or publication.producers is not self:
            raise AuditIntegrityError("feedback publication has no actual nominal issued owner")
        publication.require_call(
            recorder=publication.recorder,
            audit=publication.call.audit,
            tool_name=publication.call.tool_name,
            tool_call_id=publication.call.tool_call.id,
            messages=publication.messages,
        )
        self._require_compose_family()
        selected = self.selected
        selected.__post_init__()
        compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
        top = selected.stack.frames[-1]
        call, batch = publication.call, publication.batch
        if (
            len(compose) != 1
            or type(top) is not ToolBatchFrame
            or top.phase != "effect_reconciling"
            or top.accumulator.codec != "composer.tool-batch.v1"
            or compose[0].pending_response != top.response
            or top.next_tool_index != call.index
            or compose[0].slice_sequence != self.inventory.running.claim.segment_sequence
        ):
            raise AuditIntegrityError("feedback producer lost its driver's actual issued selection")
        authoring = batch.private_authorings_by_call[call.tool_call.id]
        found = tuple(item for item in publication.recorder.invocations if item.private_authoring is authoring)
        if len(found) != 1:
            raise AuditIntegrityError("feedback producer lost its unique actual recorded invocation")
        invocation = found[0]
        audit = call.audit
        if (
            audit is None
            or type(invocation) is not ComposerToolInvocation
            or (
                invocation.tool_call_id,
                invocation.tool_name,
                invocation.arguments_canonical,
                invocation.arguments_hash,
                invocation.authority_arguments_canonical,
                invocation.authority_arguments_hash,
                invocation.version_before,
                invocation.started_at,
                invocation.actor,
                invocation.strict_sent,
                invocation.wire_conformant,
            )
            != (
                audit.tool_call_id,
                audit.tool_name,
                audit.arguments_canonical,
                audit.arguments_hash,
                audit.authority_arguments_canonical,
                audit.authority_arguments_hash,
                audit.version_before,
                audit.started_at,
                audit.actor,
                audit.strict_sent,
                audit.wire_conformant,
            )
        ):
            raise AuditIntegrityError("feedback producer changed its actual issued audit source")
        outcomes = batch.tool_outcomes
        if len(outcomes) == call.index + 1:
            outcome = outcomes[call.index]
        elif len(outcomes) == call.index and call.tool_name == "request_interpretation_review":
            outcome = None
        else:
            raise AuditIntegrityError("feedback producer has no actual completed or session-inner output")
        recipe = capture_tool_feedback_recipe(
            response=top.response,
            completion=publication.completion,
            tool_index=call.index,
            call=call.tool_call,
            invocation=invocation,
            authoring=authoring,
            serialized=serialized,
            message=message,
            outcome=outcome,
        )
        return self._remember_feedback_provenance(
            message=message, recipe=recipe, call=call.tool_call, invocation=invocation, authoring=authoring, outcome=outcome
        )

    def _remember_feedback_provenance(
        self,
        *,
        message: dict[str, Any],
        recipe: ToolFeedbackMessageRecipe,
        call: _AdmittedToolCall,
        invocation: ComposerToolInvocation,
        authoring: PrivateToolAuthoring,
        outcome: _ToolOutcome | None,
    ) -> dict[str, Any]:
        from elspeth.web.composer.continuation_retention import require_retention_message_credentials, validate_retained_model

        if any(existing is message for existing, _, _ in self._produced) or any(
            item.message is message for item in self._feedback_produced
        ):
            raise AuditIntegrityError("message producer registered the same actual object twice")
        if (
            type(recipe) is not ToolFeedbackMessageRecipe
            or tuple(message) != ("role", "tool_call_id", "content", PROMPT_GUARD_CONTENT_PARTS_KEY)
            or type(message["role"]) is not str
            or message["role"] != "tool"
            or type(message["tool_call_id"]) is not str
            or message["tool_call_id"] != call.id
            or type(message["content"]) is not str
        ):
            raise AuditIntegrityError("feedback provenance lost its exact closed recipe/ordered keys")
        validate_retained_model(recipe)
        require_feedback_invocation(call=call, invocation=invocation, authoring=authoring, source=recipe.source, outcome=outcome)
        require_retention_message_credentials(message, env_ref_names=self.inventory.names)
        record = _FeedbackMessageProvenance(
            message,
            recipe,
            tuple(message),
            message["role"],
            message["tool_call_id"],
            message["content"],
            (
                recipe.response.codec,
                recipe.response.digest,
                recipe.response.byte_length,
                recipe.tool_index,
                recipe.tool_call_id,
                recipe.source,
            ),
            call,
            invocation,
            authoring,
            outcome,
        )
        self._remember_tool_result_provenance(message)
        self._feedback_produced.append(record)
        return message

    def _feedback_view(self, record: _FeedbackMessageProvenance) -> OrderedObjectJSON:
        from elspeth.contracts.prompt_guard import validate_prompt_guard_messages
        from elspeth.web.composer.continuation_retention import (
            OrderedObjectJSON,
            require_retention_message_credentials,
            validate_retained_model,
        )

        message, recipe = record.message, record.recipe
        if (
            type(record) is not _FeedbackMessageProvenance
            or tuple(message) != record.keys
            or tuple(message) != ("role", "tool_call_id", "content", PROMPT_GUARD_CONTENT_PARTS_KEY)
            or message["role"] is not record.role
            or record.role != "tool"
            or message["tool_call_id"] is not record.call_id
            or record.call_id != record.call.id
            or message["content"] is not record.body
            or (
                recipe.response.codec,
                recipe.response.digest,
                recipe.response.byte_length,
                recipe.tool_index,
                recipe.tool_call_id,
                recipe.source,
            )
            != record.selector
        ):
            raise AuditIntegrityError("feedback capture changed its original ordered source/field identities")
        self._require_tool_result_provenance(message)
        if record.outcome is None:
            raise AuditIntegrityError("feedback capture preceded its actual outer completion")
        validate_prompt_guard_messages((message,))
        validate_retained_model(recipe)
        require_feedback_invocation(
            call=record.call, invocation=record.invocation, authoring=record.authoring, source=recipe.source, outcome=record.outcome
        )
        require_retention_message_credentials(message, env_ref_names=self.inventory.names)
        return OrderedObjectJSON({key: value for key, value in message.items() if key not in PROMPT_GUARD_PROVENANCE_KEYS})

    def _bind_feedback_outcome(self, publication: ContinuationToolMessagePublication, outcome: _ToolOutcome) -> None:
        """Complete a private session-inner DATA pair, once, before capture."""
        if type(publication) is not ContinuationToolMessagePublication or publication.producers is not self:
            raise AuditIntegrityError("feedback completion lost its original publication owner")
        publication.require_call(
            recorder=publication.recorder,
            audit=publication.call.audit,
            tool_name=publication.call.tool_name,
            tool_call_id=publication.call.tool_call.id,
            messages=publication.messages,
        )
        call, batch = publication.call, publication.batch
        if (
            type(outcome) is not _ToolOutcome
            or len(batch.tool_outcomes) != call.index + 1
            or batch.tool_outcomes[call.index] is not outcome
            or outcome.call is not call.tool_call
        ):
            raise AuditIntegrityError("feedback completion replaced its actual outer outcome/list identity")
        entries = tuple((index, item) for index, item in enumerate(self._feedback_produced) if item.call is call.tool_call)
        if len(entries) != 1:
            raise AuditIntegrityError("feedback completion has no unique actual inner producer")
        index, record = entries[0]
        top = self.selected.stack.frames[-1]
        if (
            record.outcome is not None
            or type(top) is not ToolBatchFrame
            or top.phase != "effect_reconciling"
            or top.response != record.recipe.response
            or top.next_tool_index != call.index
            or record.recipe.tool_index != call.index
            or record.authoring is not batch.private_authorings_by_call[call.tool_call.id]
            or sum(item is record.invocation for item in publication.recorder.invocations) != 1
            or sum(item is record.message for item in publication.messages) != 1
        ):
            raise AuditIntegrityError("feedback completion changed or duplicated its original inner source")
        bound = replace(record, outcome=outcome)
        self._feedback_view(bound)
        self._feedback_produced[index] = bound

    def _require_tool_result_provenance(self, message: Mapping[str, Any]) -> None:
        entries = tuple(item for item in self._tool_result_provenance if item[0] is message)
        if len(entries) != 1:
            raise AuditIntegrityError("tool result capture has no actual live producer provenance")
        _actual, content, parts, facts = entries[0]
        if "content" not in message or PROMPT_GUARD_CONTENT_PARTS_KEY not in message:
            raise AuditIntegrityError("tool result capture lost its complete original body/partition")
        if message["content"] is not content or message[PROMPT_GUARD_CONTENT_PARTS_KEY] is not parts or len(parts) != len(facts):
            raise AuditIntegrityError("tool result capture changed its original body/partition identity")
        for part, (original, text, trust, inspection_text) in zip(parts, facts, strict=True):
            if (
                type(part) is not PromptGuardContentPart
                or part is not original
                or part.text is not text
                or part.trust is not trust
                or part.inspection_text is not inspection_text
            ):
                raise AuditIntegrityError("tool result capture changed its complete original trust partition")

    def capture_message(self, message: Mapping[str, Any]) -> tuple[str, ContinuationMessageRecipe | None]:
        feedback = tuple(item for item in self._feedback_produced if item.message is message)
        if feedback:
            from elspeth.contracts.bounded_serialization import bounded_canonical_json_size
            from elspeth.web.composer.continuation_retention import require_retention_capacity

            if len(feedback) != 1 or any(actual is message for actual, _, _ in self._produced):
                raise AuditIntegrityError("feedback capture lost its unique original registration")
            projected = self._feedback_view(feedback[0])
            measured = bounded_canonical_json_size(projected, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
            expected = require_retention_capacity(measured, reason="retained_history_bytes")
            encoded = _wire(message)
            actual = bounded_canonical_json_size(encoded, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
            if actual.admitted_bytes != expected or not projected.matches(encoded):
                raise AuditIntegrityError("feedback encoding differs from its admitted complete shelf-string representation")
            return encoded, feedback[0].recipe
        encoded = _wire(message)
        entries = tuple((recipe, original) for actual, recipe, original in self._produced if actual is message)
        if not entries:
            if PROMPT_GUARD_PROVENANCE_KEYS.intersection(message) or _NATIVE_KEYS.intersection(_retained_wire(encoded)):
                raise AuditIntegrityError("guarded/native message has no actual registered current producer")
            return encoded, None
        if len(entries) == 1 and type(entries[0][0]) in (ToolResultMessageRecipe, ToolFeedbackMessageRecipe):
            self._require_tool_result_provenance(message)
        if len(entries) != 1 or entries[0][1] != encoded:
            raise AuditIntegrityError("registered message changed its complete original wire")
        if type(entries[0][0]) is ToolCallArgumentsMessageRecipe:
            from elspeth.web.composer.continuation_tool_argument_messages import require_ordinary_argument_parts

            require_ordinary_argument_parts(message, entries[0][0].argument_indexes)
        return encoded, entries[0][0]

    def retention_message_view(self, message: Mapping[str, Any]) -> tuple[OrderedObjectJSON, ContinuationMessageRecipe | None]:
        """Validate a fresh shelf source before allocating its ordered wire.

        This grants no producer identity: the actual same-object registration,
        complete original bytes and exact argument partitions remain required.
        The ordinary capture method is still used after shelf size admission.
        """
        feedback = tuple(item for item in self._feedback_produced if item.message is message)
        if feedback:
            if len(feedback) != 1 or any(actual is message for actual, _, _ in self._produced):
                raise AuditIntegrityError("feedback retention lost its unique original registration")
            return self._feedback_view(feedback[0]), feedback[0].recipe
        from elspeth.contracts.prompt_guard import validate_prompt_guard_messages
        from elspeth.web.composer.continuation_retention import OrderedObjectJSON, validate_retained_model

        validate_prompt_guard_messages((message,))
        wire = {key: value for key, value in message.items() if key not in PROMPT_GUARD_PROVENANCE_KEYS}
        projected = OrderedObjectJSON(wire)
        # Validation continues through the complete source after saturation.
        projected.canonical_string_bytes(limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
        entries = tuple((recipe, original) for actual, recipe, original in self._produced if actual is message)
        if not entries:
            if PROMPT_GUARD_PROVENANCE_KEYS.intersection(message) or _NATIVE_KEYS.intersection(wire):
                raise AuditIntegrityError("guarded/native message has no actual registered current producer")
            return projected, None
        if len(entries) == 1 and type(entries[0][0]) in (ToolResultMessageRecipe, ToolFeedbackMessageRecipe):
            self._require_tool_result_provenance(message)
        if len(entries) != 1 or not projected.matches(entries[0][1]):
            raise AuditIntegrityError("registered message changed its complete original wire")
        recipe = entries[0][0]
        validate_retained_model(recipe)
        if type(recipe) is ToolCallArgumentsMessageRecipe:
            from elspeth.web.composer.continuation_tool_argument_messages import _ordinary_message

            expected = _ordinary_message(wire, recipe.argument_indexes)
            actual_parts = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message else None
            expected_parts = expected[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in expected else None
            if (
                any(key in message for key in PROMPT_GUARD_PROVENANCE_KEYS - {PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY})
                or actual_parts != expected_parts
            ):
                raise AuditIntegrityError("ordinary message changed its actual all-untrusted argument provenance")
        return projected, recipe

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
                NativeThinkingMessageRecipe
                | ToolCallArgumentsMessageRecipe
                | FailureSchemaMessageRecipe
                | AttachmentResultMessageRecipe
                | ToolResultMessageRecipe
                | ToolFeedbackMessageRecipe,
                str,
            ]
        ] = []
        for index, (text, recipe) in enumerate(zip(messages_json, producer_recipes, strict=True)):
            if (
                type(recipe) is NativeThinkingMessageRecipe
                or type(recipe) is ToolCallArgumentsMessageRecipe
                or type(recipe) is FailureSchemaMessageRecipe
                or type(recipe) is AttachmentResultMessageRecipe
                or type(recipe) is ToolResultMessageRecipe
                or type(recipe) is ToolFeedbackMessageRecipe
            ):
                family_positions.append(index)
                family_requests.append((recipe, text))
        # The cohort transaction ends before initial-message content-scope CAS
        # or schema policy callbacks can open their own current SQL transaction.
        family_cohort = CurrentContinuationMessageFamilies(
            self.service, self.authority, self.selected, self.inventory, finish_read=self.finish_read
        )._restore_cohort(tuple(family_requests))
        by_position = dict(zip(family_positions, family_cohort.messages, strict=True))
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
                or type(recipe) is ToolResultMessageRecipe
                or type(recipe) is ToolFeedbackMessageRecipe
            ):
                current = by_position[index]
            else:
                raise AuditIntegrityError("message recipe is outside the actual supported producer family")
            if _wire(current) != text:
                raise AuditIntegrityError("current producer differs from complete retained ordered wire")
            if type(recipe) is ToolFeedbackMessageRecipe:
                sources = tuple(source for request, source in family_cohort.feedback if request is recipe and source.message is current)
                if (
                    len(sources) != 1
                    or not any(sources[0] is item for item in family_cohort.sources)
                    or not any(current is item for item in family_cohort.messages)
                ):
                    raise AuditIntegrityError("feedback restore changed its actual reconstruction cohort/source aliases")
                source = sources[0]
                authoring = source.invocation.private_authoring
                if authoring is None or source.recipe != recipe:
                    raise AuditIntegrityError("feedback restore changed its original source selector/authoring")
                restored.append(
                    self._remember_feedback_provenance(
                        message=current,
                        recipe=recipe,
                        call=source.call,
                        invocation=source.invocation,
                        authoring=authoring,
                        outcome=source.outcome,
                    )
                )
            else:
                restored.append(self._register(current, recipe))
            if type(recipe) is ToolResultMessageRecipe:
                self._remember_tool_result_provenance(current)
        return restored


@dataclass(frozen=True, slots=True)
class ContinuationToolMessagePublication:
    """Actual live issued owners for private capture, never a write grant."""

    producers: CurrentContinuationMessageProducers
    completion: _AdmittedLLMCompletion
    batch: ContinuationBatchState
    call: ContinuationCallState
    recorder: BufferingRecorder
    messages: list[dict[str, Any]]
    catalog: PolicyCatalogView

    def require_call(
        self, *, recorder: BufferingRecorder, audit: DispatchAudit | None, tool_name: str, tool_call_id: str, messages: list[dict[str, Any]]
    ) -> None:
        from elspeth.web.composer.audit import BufferingRecorder, DispatchAudit
        from elspeth.web.composer.tool_batch import ContinuationBatchState, ContinuationCallState

        if (
            type(self.producers) is not CurrentContinuationMessageProducers
            or type(self.completion) is not _AdmittedLLMCompletion
            or type(self.batch) is not ContinuationBatchState
            or type(self.call) is not ContinuationCallState
            or type(self.recorder) is not BufferingRecorder
            or self.recorder is not recorder
            or self.messages is not messages
            or self.batch.current_call is not self.call
            or self.call.audit is not audit
            or type(audit) is not DispatchAudit
            or (self.call.tool_name, self.call.tool_call.id) != (tool_name, tool_call_id)
            or not 0 <= self.call.index < len(self.completion.tool_batch.calls)
            or self.completion.tool_batch.calls[self.call.index] is not self.call.tool_call
        ):
            raise AuditIntegrityError("tool publication changed its actual issued call/recorder/message owners")

    def register_feedback(self, *, serialized: str, message: dict[str, Any]) -> dict[str, Any]:
        return self.producers.register_tool_feedback_message(publication=self, serialized=serialized, message=message)

    def bind_completed_outcome(self, outcome: _ToolOutcome) -> None:
        self.producers._bind_feedback_outcome(self, outcome)

    def register_result(self, *, result: ToolResult, serialized: str, message: dict[str, Any]) -> dict[str, Any]:
        self.require_call(
            recorder=self.recorder,
            audit=self.call.audit,
            tool_name=self.call.tool_name,
            tool_call_id=self.call.tool_call.id,
            messages=self.messages,
        )
        return self.producers.register_tool_result_message(
            completion=self.completion,
            tool_index=self.call.index,
            call=self.call.tool_call,
            result=result,
            serialized=serialized,
            message=message,
            catalog=self.catalog,
        )


@dataclass(frozen=True, slots=True)
class CurrentMessageProduction:
    """Stable membership of actual live messages registered by their owner.

    Deep-freezing the mappings would replace the objects whose identity and
    complete original wire the producer registry must retain and verify.
    """

    producers: CurrentContinuationMessageProducers
    messages: tuple[Mapping[str, Any], ...] | None


def _produce_current_messages(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    retained_shelf: MessageShelfSnapshot | None,
    initial: bool,
    finish_read: ContinuationFinishRead | None = None,
) -> CurrentMessageProduction:
    from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, encode_message_shelf
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if type(service) is not ComposerServiceImpl or type(authority) is not ComposerAsyncOperationAuthority:
        raise AuditIntegrityError("message production requires actual service and SQL authority owners")
    if type(selected) is not SelectedContinuationState or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("message production requires actual current selection and inventory")
    if finish_read is not None and initial:
        raise AuditIntegrityError("owed-finish restoration cannot create a new initial dialogue")
    with locked_session_transaction(authority._engine, str(inventory.running.claim.session_id)) as conn:
        inputs = _read_current_inputs(conn, selected=selected, inventory=inventory, finish_read=finish_read)
    owner = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs, finish_read=finish_read)
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
    finish_read: ContinuationFinishRead | None = None,
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
            finish_read=finish_read,
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
