"""Current selected SQL owners for native and failed-schema message families.

No mutation, provider call, audit finisher or writer is replayed here. The
ordinary evidence reader retains its full current admission and journal check.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from typing import TYPE_CHECKING, Any, Self

from sqlalchemy import Connection, select

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, RetainedCompletion, decode_continuation_response, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_message_family_builders import (
    failure_schema_message,
    family_message_wire,
    rebuild_native_message,
    rebuild_tool_feedback_message,
    rebuild_tool_result_message,
)
from elspeth.web.composer.continuation_message_family_recipes import (
    AttachmentResultMessageRecipe,
    FailureSchemaMessageRecipe,
    NativeThinkingMessageRecipe,
    ToolCallArgumentsMessageRecipe,
    ToolFeedbackMessageRecipe,
    ToolResultMessageRecipe,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import _read_json_value
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import read_chunk, read_private_chunk
from elspeth.web.sessions.locking import locked_session_transaction

if TYPE_CHECKING:
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _ToolOutcome
    from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot
    from elspeth.web.composer.continuation_batch_data import RestoredBatchData
    from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence, TurnEvidenceSnapshot
    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.tools._common import ToolResult
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


@dataclass(frozen=True, slots=True)
class _BatchFamilySource:
    response: PrivateChunkRef
    data: RestoredBatchData


@dataclass(frozen=True, slots=True)
class _MessageFamilySources:
    evidence: TurnEvidenceSnapshot
    recorder: RecorderSnapshot
    responses: tuple[PrivateChunk, ...]
    batches: tuple[_BatchFamilySource, ...]
    tool_results: tuple[_ToolResultFamilySource, ...] = ()
    tool_feedback: tuple[_ToolFeedbackFamilySource, ...] = ()


@dataclass(frozen=True, slots=True)
class _CompletedToolSource:
    """Selected batch DATA and its already-admitted original completion."""

    data: RestoredBatchData
    original: AdmittedTurnEvidence


class _ToolResultFamilySource:
    """Closed bindings to the original live result and message owners."""

    __slots__ = (
        "_sealed",
        "call",
        "message",
        "recipe",
        "result",
    )

    recipe: ToolResultMessageRecipe
    call: _AdmittedToolCall
    result: ToolResult
    message: dict[str, Any]
    _sealed: bool

    def __new__(
        cls,
        recipe: ToolResultMessageRecipe,
        call: _AdmittedToolCall,
        result: ToolResult,
        message: dict[str, object],
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        recipe: ToolResultMessageRecipe,
        call: _AdmittedToolCall,
        result: ToolResult,
        message: dict[str, object],
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("_ToolResultFamilySource cannot be reinitialized")
        object.__setattr__(self, "recipe", recipe)
        object.__setattr__(self, "call", call)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("_ToolResultFamilySource bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("_ToolResultFamilySource bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not _ToolResultFamilySource or type(other) is not _ToolResultFamilySource:
            return NotImplemented
        return (
            self.recipe,
            self.call,
            self.result,
            self.message,
        ) == (
            other.recipe,
            other.call,
            other.result,
            other.message,
        )


class _ToolFeedbackFamilySource:
    """Closed bindings to the original live feedback and message owners."""

    __slots__ = (
        "_sealed",
        "call",
        "invocation",
        "message",
        "outcome",
        "recipe",
    )

    recipe: ToolFeedbackMessageRecipe
    call: _AdmittedToolCall
    outcome: _ToolOutcome
    invocation: ComposerToolInvocation
    message: dict[str, Any]
    _sealed: bool

    def __new__(
        cls,
        recipe: ToolFeedbackMessageRecipe,
        call: _AdmittedToolCall,
        outcome: _ToolOutcome,
        invocation: ComposerToolInvocation,
        message: dict[str, object],
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        recipe: ToolFeedbackMessageRecipe,
        call: _AdmittedToolCall,
        outcome: _ToolOutcome,
        invocation: ComposerToolInvocation,
        message: dict[str, object],
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("_ToolFeedbackFamilySource cannot be reinitialized")
        object.__setattr__(self, "recipe", recipe)
        object.__setattr__(self, "call", call)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "invocation", invocation)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("_ToolFeedbackFamilySource bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("_ToolFeedbackFamilySource bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not _ToolFeedbackFamilySource or type(other) is not _ToolFeedbackFamilySource:
            return NotImplemented
        return (
            self.recipe,
            self.call,
            self.outcome,
            self.invocation,
            self.message,
        ) == (
            other.recipe,
            other.call,
            other.outcome,
            other.invocation,
            other.message,
        )


class _RestoredFamilyCohort:
    """One reconstruction's original mutable message aliases; never a write grant."""

    __slots__ = (
        "_sealed",
        "feedback",
        "messages",
        "sources",
    )

    messages: tuple[dict[str, Any], ...]
    feedback: tuple[tuple[ToolFeedbackMessageRecipe, _ToolFeedbackFamilySource], ...]
    sources: tuple[_ToolFeedbackFamilySource, ...]
    _sealed: bool

    def __new__(
        cls,
        messages: tuple[dict[str, Any], ...],
        feedback: tuple[tuple[ToolFeedbackMessageRecipe, _ToolFeedbackFamilySource], ...],
        sources: tuple[_ToolFeedbackFamilySource, ...],
    ) -> Self:
        owner = object.__new__(cls)
        object.__setattr__(owner, "_sealed", False)
        return owner

    def __init__(
        self,
        messages: tuple[dict[str, Any], ...],
        feedback: tuple[tuple[ToolFeedbackMessageRecipe, _ToolFeedbackFamilySource], ...],
        sources: tuple[_ToolFeedbackFamilySource, ...],
    ) -> None:
        if self._sealed:
            raise FrozenInstanceError("_RestoredFamilyCohort cannot be reinitialized")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "feedback", feedback)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError("_RestoredFamilyCohort bindings cannot be reassigned")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError("_RestoredFamilyCohort bindings cannot be deleted")

    def __eq__(self, other: object) -> bool:
        if type(self) is not _RestoredFamilyCohort or type(other) is not _RestoredFamilyCohort:
            return NotImplemented
        return (
            self.messages,
            self.feedback,
            self.sources,
        ) == (
            other.messages,
            other.feedback,
            other.sources,
        )


def _actual_chunk(conn: Connection, running: ComposerOperationRunning, reference: PrivateChunkRef) -> PrivateChunk:
    if not 0 <= reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("message family dependency exceeds its original complete document bound")
    payload = read_private_chunk(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        reference=reference,
    )
    if len(payload) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or len(payload) != reference.byte_length:
        raise AuditIntegrityError("message family dependency changed its original complete byte bound")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("message family source is not strict UTF-8") from error
    verify_private_chunk(reference, text)
    return PrivateChunk(reference, text)


def _read_current_sources(
    conn: Connection,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    finish_read: ContinuationFinishRead | None = None,
) -> _MessageFamilySources:
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
    from elspeth.web.composer.continuation_batch_data import (
        RestoredBatchData,
        decode_coupled_batch_chunk,
        decode_memory_completion_batch_data,
        decode_prepared_batch_data,
    )
    from elspeth.web.composer.continuation_evidence import require_admitted_turn_evidence
    from elspeth.web.composer.continuation_evidence_application_data import require_selected_applied_batch_chunk
    from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection
    from elspeth.web.composer.continuation_p4_batch import decode_p4_pending_batch_chunk

    if type(selected) is not SelectedContinuationState or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("message family source requires its actual complete selection/inventory")
    running = inventory.running
    if finish_read is None:
        admitted = read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
    else:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        if type(finish_read) is not ContinuationFinishRead:
            raise AuditIntegrityError("message family received a foreign owed-read facet")
        finish_read.require_selection(selected, inventory)
        admitted = finish_read.read_evidence(conn)
    evidence = admitted.snapshot
    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("message family source lost its actual Compose recorder")
    recorder = decode_recorder_snapshot(_actual_chunk(conn, running, compose[0].audit_buffer))
    graph = require_admitted_turn_evidence(admitted, env_ref_names=inventory.names).graph
    responses: list[PrivateChunk] = []
    batches: list[_BatchFamilySource] = []

    def retain_response(reference: PrivateChunkRef) -> PrivateChunk:
        chunk = _actual_chunk(conn, running, reference)
        decode_continuation_response(chunk)
        prior = tuple(item for item in responses if item.reference == reference)
        if prior:
            if len(prior) != 1 or prior[0] != chunk:
                raise AuditIntegrityError("message family source changed its original response bytes")
            return prior[0]
        responses.append(chunk)
        return chunk

    entry = evidence.application_entry
    if entry is not None:
        if graph is None or len(entry.applications) != len(graph.cuts):
            raise AuditIntegrityError("message family source lost its actual historical cut cohort")
        for record, cut in zip(entry.applications, graph.cuts, strict=True):
            response = retain_response(record.response)
            if response != admitted.sources.chunk(record.response):
                raise AuditIntegrityError("message family history changed its actual retained response")
            batches.append(_BatchFamilySource(record.response, cut.data))
    top = selected.stack.frames[-1]
    if type(top) is ToolBatchFrame:
        response = retain_response(top.response)
        accumulator = _actual_chunk(conn, running, top.accumulator)
        selected_chunks = tuple(item for item in selected.chunks if item.reference == top.accumulator)
        if len(selected_chunks) != 1 or selected_chunks[0] != accumulator:
            raise AuditIntegrityError("message family source changed its actual selected accumulator")
        if accumulator.reference.codec == "composer.coupled-batch-data.v1":
            data = decode_coupled_batch_chunk(accumulator, env_ref_names=inventory.names)
        elif accumulator.reference.codec == "composer.p4-pending-batch.v1":
            data = decode_p4_pending_batch_chunk(accumulator, env_ref_names=inventory.names)
        elif accumulator.reference.codec == "composer.tool-batch.v1":
            payload = _read_json_value(accumulator.canonical)
            if type(payload) is not dict:
                raise AuditIntegrityError("message family selected batch is not its actual object payload")
            if payload.get("schema_version") == "composer.prepared-batch-data.v1":
                prepared = decode_prepared_batch_data(accumulator.canonical, env_ref_names=inventory.names)
                data = RestoredBatchData(prepared.batch, prepared.accumulator, prepared.calls)
            elif payload.get("schema_version") == "composer.attachment-history-batch.v1":
                from elspeth.web.composer.continuation_attachment_result_data import decode_attachment_history_batch

                if top.phase != "result_retained":
                    raise AuditIntegrityError("message family attachment history has another selected phase")
                retained = decode_attachment_history_batch(accumulator, env_ref_names=inventory.names)
                prepared = decode_prepared_batch_data(retained.prepared_json, env_ref_names=inventory.names)
                data = RestoredBatchData(prepared.batch, prepared.accumulator, prepared.calls)
            elif payload.get("schema_version") == "composer.memory-completion-batch-data.v1":
                completed = decode_memory_completion_batch_data(accumulator.canonical, env_ref_names=inventory.names)
                data = RestoredBatchData(completed.batch, completed.accumulator, completed.calls)
            else:
                raise AuditIntegrityError("message family source has no implemented selected batch payload")
        elif accumulator.reference.codec == "composer.applied-p4-batch.v2":
            require_selected_applied_batch_chunk(accumulator, evidence=admitted, env_ref_names=inventory.names)
            if graph is None:
                raise AuditIntegrityError("message family source lost its selected applied graph")
            data = graph.selected
        else:
            raise AuditIntegrityError("message family source has no implemented selected batch codec")
        if data.calls != decode_continuation_response(response).tool_batch.calls or top.next_tool_index != data.batch.next_tool_index:
            raise AuditIntegrityError("message family source changed its actual call/cursor join")
        batches.append(_BatchFamilySource(top.response, data))
    # Closed selected recipes name exact historical provider sources. The
    # current frame may already hold a later provider effect; neither ordinal
    # order nor the newest response substitutes for this original source.
    from elspeth.web.sessions.continuation_primary import (
        read_committed_primary_response_for_settlement_on_connection,
        read_committed_primary_response_on_connection,
    )

    checked_native: set[PrivateChunkRef] = set()
    for recipe in evidence.transcript.producer_recipes or ():
        if (
            type(recipe) is NativeThinkingMessageRecipe
            or type(recipe) is ToolCallArgumentsMessageRecipe
            or type(recipe) is ToolResultMessageRecipe
            or type(recipe) is ToolFeedbackMessageRecipe
        ) and recipe.response not in checked_native:
            read_response = (
                read_committed_primary_response_on_connection
                if finish_read is None
                else read_committed_primary_response_for_settlement_on_connection
            )
            actual = read_response(conn, running, selected=selected, reference=recipe.response)
            if retain_response(recipe.response) != actual:
                raise AuditIntegrityError("native recipe changed its actual committed paid response source")
            checked_native.add(recipe.response)
    result_sources = _admit_selected_tool_result_recipes_on_connection(
        conn, running, selected=selected, admitted=admitted, names=inventory.names
    )
    feedback_sources = _admit_selected_tool_feedback_recipes_on_connection(
        conn, running, selected=selected, admitted=admitted, names=inventory.names
    )
    return _MessageFamilySources(evidence, recorder, tuple(responses), tuple(batches), result_sources, feedback_sources)


def _require_completed_tool_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    admitted: AdmittedTurnEvidence,
    recipe: ToolResultMessageRecipe | ToolFeedbackMessageRecipe,
    names: frozenset[str],
) -> _CompletedToolSource:
    """Prove a committed producer source, never bless the selected shelf text."""
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_evidence import require_admitted_turn_evidence
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
    from elspeth.web.composer.continuation_phase_transitions import (
        read_tool_issue_source_on_connection,
        require_same_sequence_issue_history_on_connection,
        validate_tool_effects_on_connection,
    )
    from elspeth.web.composer.tools._common import ToolResult
    from elspeth.web.sessions.continuation_selection import load_private_chunks
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import composer_continuation_events_table as events

    if type(recipe) not in (ToolResultMessageRecipe, ToolFeedbackMessageRecipe):
        raise AuditIntegrityError("tool result source replaced its closed selector")
    graph = require_admitted_turn_evidence(admitted, env_ref_names=names).graph
    if graph is None:
        raise AuditIntegrityError("tool result source lost its one actual shared graph")
    candidates: list[_BatchFamilySource] = []
    entry = admitted.snapshot.application_entry
    if entry is not None:
        if len(entry.applications) != len(graph.cuts):
            raise AuditIntegrityError("tool result source lost its actual application history")
        for application, cut in zip(entry.applications, graph.cuts, strict=True):
            candidates.append(_BatchFamilySource(application.response, cut.data))
    top = selected.stack.frames[-1]
    if type(top) is ToolBatchFrame:
        candidates.append(_BatchFamilySource(top.response, graph.selected))
    matches: list[_BatchFamilySource] = []
    for candidate in candidates:
        if candidate.response != recipe.response:
            continue
        data = candidate.data
        if not recipe.tool_index < len(data.batch.tool_outcomes):
            continue
        call, outcome = data.calls[recipe.tool_index], data.batch.tool_outcomes[recipe.tool_index]
        if (
            call.id != recipe.tool_call_id
            or outcome.call is not call
            or (type(recipe) is ToolResultMessageRecipe and (outcome.error_class is not None or type(outcome.response) is not ToolResult))
        ):
            raise AuditIntegrityError("tool result selector changed its actual completed call/result")
        if matches:
            prior = matches[0].data
            if prior.calls[recipe.tool_index] is not call or prior.batch.tool_outcomes[recipe.tool_index].response is not outcome.response:
                raise AuditIntegrityError("tool result source replaced its original shared call/result aliases")
            if type(recipe) is ToolFeedbackMessageRecipe and prior.batch.tool_outcomes[recipe.tool_index] is not outcome:
                raise AuditIntegrityError("feedback source replaced its original shared outcome alias")
        matches.append(candidate)
    if not matches:
        raise AuditIntegrityError("tool result recipe has no actual selected completed result")
    data = matches[0].data
    key = f"tool:{recipe.response.digest}:{recipe.tool_index}"
    issue = read_tool_issue_source_on_connection(conn, running, effect_key=key, env_ref_names=names)
    require_same_sequence_issue_history_on_connection(conn, running, selected=selected, issue=issue)
    if (issue.response, issue.tool_index, issue.tool_call_id) != (recipe.response, recipe.tool_index, recipe.tool_call_id):
        raise AuditIntegrityError("tool result source changed its actual immutable issue selector")
    validate_tool_effects_on_connection(conn, running, selected=selected, env_ref_names=names)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == key)
    ).one_or_none()
    cursor = issue.cursor + 1
    event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == cursor)
    ).one_or_none()
    checkpoint = conn.execute(
        select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor == cursor)
    ).one_or_none()
    if (
        effect is None
        or effect.owner != "tool_dispatch"
        or effect.disposition != "committed"
        or event is None
        or event.tag != "checkpoint"
        or checkpoint is None
        or checkpoint.kind != "transition"
        or cursor > selected.stack.cursor
    ):
        raise AuditIntegrityError("tool result has no exact committed result/event/checkpoint")
    root = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=operation, key=checkpoint.snapshot_chunk_key).decode("utf-8", errors="strict")
    )
    frame = root.frames[-1]
    if (
        root.logical != selected.stack.logical
        or root.cursor != cursor
        or type(frame) is not ToolBatchFrame
        or frame.phase != "tool_ready"
        or frame.response != recipe.response
        or frame.accumulator.codec != "composer.coupled-batch-data.v1"
        or frame.next_tool_index != recipe.tool_index + 1
        or key not in frame.settled_effect_ids
        or effect.result_chunk_key != f"value:{frame.accumulator.codec}:{frame.accumulator.digest}"
        or effect.receipt_key != f"tool-result:{frame.accumulator.digest}"
        or tuple(item for item in root.effect_frontier if item.effect_id == key)
        != (EffectFrontierEntry(effect_id=key, disposition="committed", receipt_id=effect.receipt_key),)
    ):
        raise AuditIntegrityError("tool result changed its exact committed source/receipt locator")
    expected = canonical_json(
        {
            "owner": "composer_tool_phase",
            "transition": "tool_completed",
            "effect_key": key,
            "response": recipe.response.digest,
            "batch": frame.accumulator.digest,
        }
    ).encode("utf-8")
    if read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key) != expected:
        raise AuditIntegrityError("tool result changed its original completion event")
    completed = decode_coupled_batch_chunk(_actual_chunk(conn, running, frame.accumulator), env_ref_names=names)
    if completed.calls != data.calls or completed.batch.tool_outcomes != data.batch.tool_outcomes[: recipe.tool_index + 1]:
        raise AuditIntegrityError("tool result differs from its actual committed completed prefix")
    historical = SelectedContinuationState(
        checkpoint.checkpoint_id, root, load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root)
    )
    historical.__post_init__()
    original = require_admitted_turn_evidence(
        read_selected_turn_evidence_graph_on_connection(conn, running, selected=historical, names=names), env_ref_names=names
    )
    return _CompletedToolSource(data, original)


def _require_tool_result_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    admitted: AdmittedTurnEvidence,
    recipe: ToolResultMessageRecipe,
    names: frozenset[str],
) -> _ToolResultFamilySource:
    from elspeth.web.composer.tools._common import ToolResult

    if type(recipe) is not ToolResultMessageRecipe:
        raise AuditIntegrityError("tool result source replaced its closed selector")
    completed = _require_completed_tool_source_on_connection(
        conn, running, selected=selected, admitted=admitted, recipe=recipe, names=names
    )
    data = completed.data
    call, outcome = data.calls[recipe.tool_index], data.batch.tool_outcomes[recipe.tool_index]
    if type(outcome.response) is not ToolResult:
        raise AuditIntegrityError("tool result source lost its nominal retained result")
    if outcome.response.plugin_schemas is not None:
        raise AuditIntegrityError("schema-augmented result lost its existing current-policy recipe family")
    message = rebuild_tool_result_message(call=call, result=outcome.response)
    from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY

    if any(part.trust is not None for part in message[PROMPT_GUARD_CONTENT_PARTS_KEY]):
        raise AuditIntegrityError("reference-bearing result lost its existing current-policy recipe family")
    return _ToolResultFamilySource(recipe, call, outcome.response, message)


def _require_tool_feedback_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    admitted: AdmittedTurnEvidence,
    recipe: ToolFeedbackMessageRecipe,
    names: frozenset[str],
    text: str,
) -> _ToolFeedbackFamilySource:
    if type(recipe) is not ToolFeedbackMessageRecipe:
        raise AuditIntegrityError("feedback source replaced its closed selector")
    completed = _require_completed_tool_source_on_connection(
        conn, running, selected=selected, admitted=admitted, recipe=recipe, names=names
    )
    data = completed.data
    graph = admitted.graph
    if graph is None:
        raise AuditIntegrityError("feedback source lost its original admitted graph")
    call, outcome = data.calls[recipe.tool_index], data.batch.tool_outcomes[recipe.tool_index]
    if data.batch.retained_failure is not None and recipe.tool_index == len(data.batch.tool_outcomes) - 1:
        raise AuditIntegrityError("primary terminal output cannot impersonate ordinary feedback")
    authoring = data.batch.private_authorings_by_call[call.id]
    invocations = tuple(item for item in graph.recorder.invocations if item.private_authoring is authoring)
    if len(invocations) != 1:
        raise AuditIntegrityError("feedback source has no unique original invocation/authoring alias")
    invocation = invocations[0]
    from elspeth.contracts.hashing import canonical_json

    if invocation.arguments_canonical != canonical_json(data.batch.decoded_args_by_call_id[call.id]):
        raise AuditIntegrityError("feedback changed its actual selected audit arguments")
    message = rebuild_tool_feedback_message(
        call=call, invocation=invocation, authoring=authoring, outcome=outcome, recipe=recipe, selected_message_json=text
    )
    original_graph = completed.original.graph
    if original_graph is None:
        raise AuditIntegrityError("feedback original completion lost its admitted graph")
    original_data = original_graph.selected
    if not recipe.tool_index < len(original_data.calls) or not recipe.tool_index < len(original_data.batch.tool_outcomes):
        raise AuditIntegrityError("feedback original completion lost its actual call/outcome")
    original_call = original_data.calls[recipe.tool_index]
    original_outcome = original_data.batch.tool_outcomes[recipe.tool_index]
    original_authoring = original_data.batch.private_authorings_by_call.get(original_call.id)
    if original_authoring is None or original_outcome.call is not original_call:
        raise AuditIntegrityError("feedback original completion lost its actual call/authoring aliases")
    original_invocations = tuple(item for item in original_graph.recorder.invocations if item.private_authoring is original_authoring)
    if len(original_invocations) != 1:
        raise AuditIntegrityError("feedback original completion has no unique invocation/authoring alias")
    # Each graph must retain its own aliases. Across independent decodes,
    # compare the complete immutable audit record by value, including private
    # authoring, result/arguments/authority hashes, status, times and actor.
    if original_call != call or original_outcome != outcome or original_invocations[0] != invocation:
        raise AuditIntegrityError("feedback invocation differs from its original completion")
    original_shelf = completed.original.snapshot.transcript
    original_messages = tuple(
        (saved, original_text)
        for saved, original_text in zip(original_shelf.producer_recipes, original_shelf.messages_json, strict=True)
        if type(saved) is ToolFeedbackMessageRecipe
        and (saved.response, saved.tool_index, saved.tool_call_id) == (recipe.response, recipe.tool_index, recipe.tool_call_id)
    )
    if len(original_messages) != 1 or original_messages[0][0] != recipe:
        raise AuditIntegrityError("feedback original completion lost its exact recipe/message")
    if original_messages[0][1] != text:
        raise AuditIntegrityError("feedback ordered wire differs from its original completion")
    return _ToolFeedbackFamilySource(recipe, call, outcome, invocation, message)


def _admit_selected_tool_feedback_recipes_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    admitted: AdmittedTurnEvidence,
    names: frozenset[str],
) -> tuple[_ToolFeedbackFamilySource, ...]:
    """Fresh selected source proof, also used by current input admission.

    This uses the already admitted graph and the canonical issue/history
    reader; it never recursively admits message producers or renews a budget.
    """
    from elspeth.web.composer.continuation_evidence import require_admitted_turn_evidence

    transcript = require_admitted_turn_evidence(admitted, env_ref_names=names).snapshot.transcript
    if not transcript.producer_recipes:
        return ()
    if len(transcript.producer_recipes) != len(transcript.messages_json):
        raise AuditIntegrityError("feedback admission lost its complete selected producer positions")
    sources = []
    for recipe, text in zip(transcript.producer_recipes, transcript.messages_json, strict=True):
        if type(recipe) is ToolFeedbackMessageRecipe:
            source = _require_tool_feedback_source_on_connection(
                conn, running, selected=selected, admitted=admitted, recipe=recipe, names=names, text=text
            )
            if family_message_wire(source.message) != text:
                raise AuditIntegrityError("feedback differs from complete original selected wire/body hash")
            sources.append(source)
    return tuple(sources)


def _admit_selected_tool_result_recipes_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    admitted: AdmittedTurnEvidence,
    names: frozenset[str],
) -> tuple[_ToolResultFamilySource, ...]:
    """Validate new recipe bodies after the caller's fresh current SQL read.

    Used by current input projection as well as cold restoration. A private
    capture entry cannot make an uncommitted or foreign result admissible.
    """
    from elspeth.web.composer.continuation_evidence import require_admitted_turn_evidence

    transcript = require_admitted_turn_evidence(admitted, env_ref_names=names).snapshot.transcript
    if not transcript.producer_recipes:
        return ()
    if len(transcript.producer_recipes) != len(transcript.messages_json):
        raise AuditIntegrityError("tool result admission lost its complete selected producer positions")
    result: list[_ToolResultFamilySource] = []
    for recipe, text in zip(transcript.producer_recipes, transcript.messages_json, strict=True):
        if type(recipe) is not ToolResultMessageRecipe:
            continue
        source = _require_tool_result_source_on_connection(conn, running, selected=selected, admitted=admitted, recipe=recipe, names=names)
        if family_message_wire(source.message) != text:
            raise AuditIntegrityError("tool result producer differs from complete original selected wire/body hash")
        result.append(source)
    return tuple(result)


def _tool_result_source(sources: _MessageFamilySources, recipe: ToolResultMessageRecipe) -> tuple[_AdmittedToolCall, ToolResult]:
    found = tuple(item for item in sources.tool_results if item.recipe == recipe)
    if not found:
        raise AuditIntegrityError("tool result has no current committed producer admission")
    first = found[0]
    if any(item.call is not first.call or item.result is not first.result for item in found):
        raise AuditIntegrityError("tool result recipe changed its actual admitted source aliases")
    return first.call, first.result


def _response(sources: _MessageFamilySources, reference: PrivateChunkRef) -> PrivateChunk:
    found = tuple(chunk for chunk in sources.responses if chunk.reference == reference)
    if len(found) != 1:
        raise AuditIntegrityError("message family recipe has no actual selected/historical response source")
    return found[0]


def _require_selected_message(sources: _MessageFamilySources, text: str) -> None:
    if text not in sources.evidence.transcript.messages_json:
        raise AuditIntegrityError("message family changed its complete original selected shelf wire")


def _require_response_audit_source(sources: _MessageFamilySources, response: PrivateChunk) -> None:
    from elspeth.web.composer.continuation_provider_outcomes import (
        decode_physical_audit_call,
        encode_physical_audit_call,
        require_successful_response_audit,
    )

    retained = RetainedCompletion.model_validate_json(response.canonical)
    if retained.audit_call_json is None:
        raise AuditIntegrityError("native message source requires its actual retained physical audit")
    audit = decode_physical_audit_call(retained.audit_call_json)
    if type(audit.call_id) is not str or not audit.call_id:
        raise AuditIntegrityError("native message source lost its actual physical call identity")
    actual = tuple(call for call in sources.recorder.llm_calls if call.call_id == audit.call_id)
    if len(actual) != 1 or encode_physical_audit_call(actual[0]) != retained.audit_call_json:
        raise AuditIntegrityError("native message source differs from its actual original recorder call")
    completion = decode_continuation_response(response)
    require_successful_response_audit(
        audit,
        completion.provider_metadata,
        attempt_id=audit.call_id,
        started_at=audit.started_at,
        finished_at=audit.finished_at,
    )


def _require_native_audit_source(sources: _MessageFamilySources, response: PrivateChunk) -> None:
    retained = RetainedCompletion.model_validate_json(response.canonical)
    if retained.native_thinking_json is None:
        raise AuditIntegrityError("native message source requires its original ordered provider input")
    _require_response_audit_source(sources, response)


def _failure_source(sources: _MessageFamilySources, recipe: FailureSchemaMessageRecipe) -> tuple[_AdmittedToolCall, ToolResult]:
    from elspeth.web.composer.tools._common import ToolResult

    _response(sources, recipe.response)
    found: list[tuple[_AdmittedToolCall, ToolResult]] = []
    for source in sources.batches:
        if source.response != recipe.response:
            continue
        data = source.data
        # The retained live owner can have appended its completed outcome
        # immediately before the outer driver advances next_tool_index.
        # Its actual coupled codec admits that exact current-call phase.
        # Read the complete recorded outcome prefix, never discard its tail.
        prefix = data.calls[: len(data.batch.tool_outcomes)]
        for call, outcome in zip(prefix, data.batch.tool_outcomes, strict=True):
            if call.id != recipe.tool_call_id:
                continue
            if outcome.call != call or outcome.error_class is not None or type(outcome.response) is not ToolResult:
                raise AuditIntegrityError("failure schema source has no actual complete business result")
            if outcome.response.success or outcome.response.plugin_schemas is None:
                raise AuditIntegrityError("failure schema source lost its actual failed augmentation")
            if found and (found[0][0] is not call or found[0][1] is not outcome.response):
                raise AuditIntegrityError("failure schema source changed an original shared call/result identity")
            found.append((call, outcome.response))
    if not found:
        raise AuditIntegrityError("failure schema recipe has no actual selected completed call")
    return found[0]


def _attachment_source(sources: _MessageFamilySources, recipe: AttachmentResultMessageRecipe) -> tuple[_AdmittedToolCall, ToolResult]:
    from elspeth.web.composer.tools._common import ToolResult

    _response(sources, recipe.response)
    found: list[tuple[_AdmittedToolCall, ToolResult]] = []
    for source in sources.batches:
        if source.response != recipe.response:
            continue
        data = source.data
        for call, outcome in zip(data.calls[: len(data.batch.tool_outcomes)], data.batch.tool_outcomes, strict=True):
            if call.id != recipe.tool_call_id:
                continue
            if (
                call.function.name not in ("read_attachment", "get_blob_content")
                or outcome.call != call
                or outcome.error_class is not None
                or type(outcome.response) is not ToolResult
            ):
                raise AuditIntegrityError("attachment message lost its actual complete recorded result")
            if found and (found[0][0] is not call or found[0][1] is not outcome.response):
                raise AuditIntegrityError("attachment message changed its original shared call/result")
            found.append((call, outcome.response))
    if not found:
        raise AuditIntegrityError("attachment message has no actual selected completed read")
    return found[0]


@dataclass(frozen=True, slots=True)
class CurrentContinuationMessageFamilies:
    service: ComposerServiceImpl
    authority: ComposerAsyncOperationAuthority
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    finish_read: ContinuationFinishRead | None = None

    def __post_init__(self) -> None:
        from elspeth.web.composer.continuation_evidence_restore import _require_current_reader_service
        from elspeth.web.composer.service import ComposerServiceImpl
        from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

        if (
            type(self.service) is not ComposerServiceImpl
            or type(self.authority) is not ComposerAsyncOperationAuthority
            or type(self.selected) is not SelectedContinuationState
            or type(self.inventory) is not CurrentContinuationEnvInventory
        ):
            raise AuditIntegrityError("message families require their actual current nominal owners")
        _require_current_reader_service(self.service, self.authority)
        if self.finish_read is not None:
            from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

            if type(self.finish_read) is not ContinuationFinishRead:
                raise AuditIntegrityError("message families require their nominal owed-read facet")
            self.finish_read.require_selection(self.selected, self.inventory)

    def _read(self) -> _MessageFamilySources:
        with locked_session_transaction(self.authority._engine, str(self.inventory.running.claim.session_id)) as conn:
            return _read_current_sources(conn, selected=self.selected, inventory=self.inventory, finish_read=self.finish_read)

    def native_message(self, *, recipe: NativeThinkingMessageRecipe, selected_message_json: str) -> dict[str, Any]:
        if type(recipe) is not NativeThinkingMessageRecipe:
            raise AuditIntegrityError("native reconstruction replaced its closed source recipe")
        before = self._read()
        _require_selected_message(before, selected_message_json)
        response = _response(before, recipe.response)
        _require_native_audit_source(before, response)
        message = rebuild_native_message(response=response, recipe=recipe, selected_message_json=selected_message_json)
        after = self._read()
        _require_selected_message(after, selected_message_json)
        if _response(after, recipe.response) != response:
            raise AuditIntegrityError("native message source changed during current reconstruction")
        _require_native_audit_source(after, response)
        return message

    def tool_argument_message(self, *, recipe: ToolCallArgumentsMessageRecipe, selected_message_json: str) -> dict[str, Any]:
        from elspeth.web.composer.continuation_tool_argument_messages import rebuild_tool_argument_message

        if type(recipe) is not ToolCallArgumentsMessageRecipe:
            raise AuditIntegrityError("ordinary argument reconstruction replaced its closed source recipe")
        before = self._read()
        _require_selected_message(before, selected_message_json)
        response = _response(before, recipe.response)
        _require_response_audit_source(before, response)
        message = rebuild_tool_argument_message(response=response, recipe=recipe, selected_message_json=selected_message_json)
        after = self._read()
        _require_selected_message(after, selected_message_json)
        if _response(after, recipe.response) != response:
            raise AuditIntegrityError("ordinary argument source changed during current reconstruction")
        _require_response_audit_source(after, response)
        return message

    def failure_message(self, *, recipe: FailureSchemaMessageRecipe, selected_message_json: str) -> dict[str, Any]:
        if type(recipe) is not FailureSchemaMessageRecipe:
            raise AuditIntegrityError("failure reconstruction replaced its closed source recipe")
        before = self._read()
        _require_selected_message(before, selected_message_json)
        call, result = _failure_source(before, recipe)
        _snapshot, catalog = self.service._policy_context.build(before.evidence.actor_user_id)
        message = failure_schema_message(call=call, result=result, catalog=catalog)
        if family_message_wire(message) != selected_message_json:
            raise AuditIntegrityError("current failure schema producer differs from complete original selected wire")
        after = self._read()
        _require_selected_message(after, selected_message_json)
        current_call, current_result = _failure_source(after, recipe)
        if current_call != call or current_result != result:
            raise AuditIntegrityError("failure schema source changed during current reconstruction")
        return message

    def attachment_message(self, *, recipe: AttachmentResultMessageRecipe, selected_message_json: str) -> dict[str, Any]:
        from elspeth.web.composer.continuation_message_family_builders import attachment_result_message

        if type(recipe) is not AttachmentResultMessageRecipe:
            raise AuditIntegrityError("attachment reconstruction replaced its closed original source recipe")
        before = self._read()
        _require_selected_message(before, selected_message_json)
        call, result = _attachment_source(before, recipe)
        message = attachment_result_message(call=call, result=result)
        if family_message_wire(message) != selected_message_json:
            raise AuditIntegrityError("attachment result producer differs from complete original selected wire")
        after = self._read()
        _require_selected_message(after, selected_message_json)
        current_call, current_result = _attachment_source(after, recipe)
        if current_call != call or current_result != result:
            raise AuditIntegrityError("attachment result source changed during current reconstruction")
        return message

    def restore_messages(
        self,
        requests: tuple[
            tuple[
                NativeThinkingMessageRecipe
                | ToolCallArgumentsMessageRecipe
                | FailureSchemaMessageRecipe
                | AttachmentResultMessageRecipe
                | ToolResultMessageRecipe
                | ToolFeedbackMessageRecipe,
                str,
            ],
            ...,
        ],
    ) -> tuple[dict[str, Any], ...]:
        return self._restore_cohort(requests).messages

    def _restore_cohort(
        self,
        requests: tuple[
            tuple[
                NativeThinkingMessageRecipe
                | ToolCallArgumentsMessageRecipe
                | FailureSchemaMessageRecipe
                | AttachmentResultMessageRecipe
                | ToolResultMessageRecipe
                | ToolFeedbackMessageRecipe,
                str,
            ],
            ...,
        ],
    ) -> _RestoredFamilyCohort:
        """Rebuild a complete shelf's source-backed cohort in one locked read.

        Policy acquisition stays outside the session lock: its configured
        snapshot supplier may perform its own database work. Only the existing
        response/message projections run inside. The private DATA cohort keeps
        actual rebuilt messages and source aliases for its restoration caller;
        it carries no transaction, lease, read or dispatch capability. Final
        reads recheck live custody.
        """
        from elspeth.web.composer.continuation_message_family_builders import attachment_result_message
        from elspeth.web.composer.continuation_tool_argument_messages import rebuild_tool_argument_message

        if type(requests) is not tuple:
            raise AuditIntegrityError("family cohort requires its complete requests")
        catalogs: dict[int, PolicyCatalogView] = {}
        policy_sources: _MessageFamilySources | None = None
        for index, request in enumerate(requests):
            if type(request) is not tuple or len(request) != 2 or type(request[1]) is not str:
                raise AuditIntegrityError("family cohort replaced its complete request")
            recipe, _text = request
            if type(recipe) is FailureSchemaMessageRecipe:
                if policy_sources is None:
                    policy_sources = self._read()
                _snapshot, catalog = self.service._policy_context.build(policy_sources.evidence.actor_user_id)
                catalogs[index] = catalog
            elif (
                type(recipe) is not NativeThinkingMessageRecipe
                and type(recipe) is not ToolCallArgumentsMessageRecipe
                and type(recipe) is not AttachmentResultMessageRecipe
                and type(recipe) is not ToolResultMessageRecipe
                and type(recipe) is not ToolFeedbackMessageRecipe
            ):
                raise AuditIntegrityError("family cohort contains an unsupported source recipe")
        if not requests:
            return _RestoredFamilyCohort((), (), ())
        with locked_session_transaction(self.authority._engine, str(self.inventory.running.claim.session_id)) as conn:
            before = _read_current_sources(conn, selected=self.selected, inventory=self.inventory, finish_read=self.finish_read)
            if policy_sources is not None and policy_sources != before:
                raise AuditIntegrityError("family cohort changed during current policy acquisition")
            messages: list[dict[str, Any]] = []
            feedback: list[tuple[ToolFeedbackMessageRecipe, _ToolFeedbackFamilySource]] = []
            for index, (recipe, text) in enumerate(requests):
                _require_selected_message(before, text)
                if type(recipe) is NativeThinkingMessageRecipe:
                    response = _response(before, recipe.response)
                    _require_native_audit_source(before, response)
                    message = rebuild_native_message(response=response, recipe=recipe, selected_message_json=text)
                elif type(recipe) is ToolCallArgumentsMessageRecipe:
                    response = _response(before, recipe.response)
                    _require_response_audit_source(before, response)
                    message = rebuild_tool_argument_message(response=response, recipe=recipe, selected_message_json=text)
                elif type(recipe) is FailureSchemaMessageRecipe:
                    call, result = _failure_source(before, recipe)
                    message = failure_schema_message(call=call, result=result, catalog=catalogs[index])
                elif type(recipe) is AttachmentResultMessageRecipe:
                    call, result = _attachment_source(before, recipe)
                    message = attachment_result_message(call=call, result=result)
                elif type(recipe) is ToolResultMessageRecipe:
                    _require_response_audit_source(before, _response(before, recipe.response))
                    admitted_results = tuple(item for item in before.tool_results if item.recipe == recipe)
                    if not admitted_results:
                        raise AuditIntegrityError("tool result lost its freshly admitted producer")
                    message = admitted_results[0].message
                elif type(recipe) is ToolFeedbackMessageRecipe:
                    _require_response_audit_source(before, _response(before, recipe.response))
                    admitted_feedback = tuple(item for item in before.tool_feedback if item.recipe == recipe)
                    if not admitted_feedback:
                        raise AuditIntegrityError("feedback lost its freshly admitted committed producer")
                    message = admitted_feedback[0].message
                    feedback.append((recipe, admitted_feedback[0]))
                else:
                    raise AuditIntegrityError("family cohort lost its admitted recipe")
                if family_message_wire(message) != text:
                    raise AuditIntegrityError("family cohort producer differs from complete original selected wire")
                messages.append(message)
            after = _read_current_sources(conn, selected=self.selected, inventory=self.inventory, finish_read=self.finish_read)
            # Both are actual full readers. Equality does not grant admission;
            # it binds their complete selected cohorts across pure rebuilding.
            if before != after:
                raise AuditIntegrityError("family cohort changed during current reconstruction")
            for recipe, text in requests:
                _require_selected_message(after, text)
                if type(recipe) is NativeThinkingMessageRecipe:
                    response = _response(before, recipe.response)
                    if _response(after, recipe.response) != response:
                        raise AuditIntegrityError("native message source changed during current reconstruction")
                    _require_native_audit_source(after, response)
                elif type(recipe) is ToolCallArgumentsMessageRecipe:
                    response = _response(before, recipe.response)
                    if _response(after, recipe.response) != response:
                        raise AuditIntegrityError("ordinary argument source changed during current reconstruction")
                    _require_response_audit_source(after, response)
                elif type(recipe) is FailureSchemaMessageRecipe:
                    if _failure_source(after, recipe) != _failure_source(before, recipe):
                        raise AuditIntegrityError("failure schema source changed during current reconstruction")
                elif type(recipe) is AttachmentResultMessageRecipe:
                    if _attachment_source(after, recipe) != _attachment_source(before, recipe):
                        raise AuditIntegrityError("attachment result source changed during current reconstruction")
                elif type(recipe) is ToolResultMessageRecipe:
                    if _tool_result_source(after, recipe) != _tool_result_source(before, recipe):
                        raise AuditIntegrityError("tool result source changed during current reconstruction")
                    _require_response_audit_source(after, _response(after, recipe.response))
                elif type(recipe) is ToolFeedbackMessageRecipe:
                    prior = tuple(item for item in before.tool_feedback if item.recipe == recipe)
                    current = tuple(item for item in after.tool_feedback if item.recipe == recipe)
                    if not prior or not current or prior != current:
                        raise AuditIntegrityError("feedback source changed during current reconstruction")
                    _require_response_audit_source(after, _response(after, recipe.response))
            return _RestoredFamilyCohort(tuple(messages), tuple(feedback), before.tool_feedback)
