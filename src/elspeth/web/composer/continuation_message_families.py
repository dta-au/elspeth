"""Current selected SQL owners for native and failed-schema message families.

No mutation, provider call, audit finisher or writer is replayed here. The
ordinary evidence reader retains its full current admission and journal check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, RetainedCompletion, decode_continuation_response, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_message_family_builders import (
    failure_schema_message,
    family_message_wire,
    rebuild_native_message,
)
from elspeth.web.composer.continuation_message_family_recipes import (
    AttachmentResultMessageRecipe,
    FailureSchemaMessageRecipe,
    NativeThinkingMessageRecipe,
    ToolCallArgumentsMessageRecipe,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import _read_json_value
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.locking import locked_session_transaction

if TYPE_CHECKING:
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall
    from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot
    from elspeth.web.composer.continuation_batch_data import RestoredBatchData
    from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot
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


def _actual_chunk(conn: Connection, running: ComposerOperationRunning, reference: PrivateChunkRef) -> PrivateChunk:
    try:
        text = read_chunk(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            key=f"value:{reference.codec}:{reference.digest}",
        ).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("message family source is not strict UTF-8") from error
    verify_private_chunk(reference, text)
    return PrivateChunk(reference, text)


def _read_current_sources(
    conn: Connection, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> _MessageFamilySources:
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
    from elspeth.web.composer.continuation_batch_data import (
        RestoredBatchData,
        decode_coupled_batch_chunk,
        decode_memory_completion_batch_data,
        decode_prepared_batch_data,
    )
    from elspeth.web.composer.continuation_evidence_application_data import (
        require_evidence_application_links,
        require_selected_applied_batch_chunk,
    )
    from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection
    from elspeth.web.composer.continuation_p4_batch import decode_p4_pending_batch_chunk

    if type(selected) is not SelectedContinuationState or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("message family source requires its actual complete selection/inventory")
    running = inventory.running
    evidence = read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("message family source lost its actual Compose recorder")
    recorder = decode_recorder_snapshot(_actual_chunk(conn, running, compose[0].audit_buffer))
    graph = require_evidence_application_links(evidence, env_ref_names=inventory.names)
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
            if response.canonical != record.response_json:
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
        elif accumulator.reference.codec == "composer.applied-p4-batch.v1":
            require_selected_applied_batch_chunk(accumulator, evidence=evidence, env_ref_names=inventory.names)
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
    from elspeth.web.sessions.continuation_primary import read_committed_primary_response_on_connection

    checked_native: set[PrivateChunkRef] = set()
    for recipe in evidence.transcript.producer_recipes or ():
        if (
            type(recipe) is NativeThinkingMessageRecipe or type(recipe) is ToolCallArgumentsMessageRecipe
        ) and recipe.response not in checked_native:
            actual = read_committed_primary_response_on_connection(conn, running, selected=selected, reference=recipe.response)
            if retain_response(recipe.response) != actual:
                raise AuditIntegrityError("native recipe changed its actual committed paid response source")
            checked_native.add(recipe.response)
    return _MessageFamilySources(evidence, recorder, tuple(responses), tuple(batches))


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

    def _read(self) -> _MessageFamilySources:
        with locked_session_transaction(self.authority._engine, str(self.inventory.running.claim.session_id)) as conn:
            return _read_current_sources(conn, selected=self.selected, inventory=self.inventory)

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
                NativeThinkingMessageRecipe | ToolCallArgumentsMessageRecipe | FailureSchemaMessageRecipe | AttachmentResultMessageRecipe,
                str,
            ],
            ...,
        ],
    ) -> tuple[dict[str, Any], ...]:
        """Rebuild a complete shelf's source-backed cohort in one locked read.

        Policy acquisition stays outside the session lock: its configured
        snapshot supplier may perform its own database work. Only the existing
        response/message projections run inside. No source cohort escapes or
        survives this invocation, and final reads recheck live custody.
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
            ):
                raise AuditIntegrityError("family cohort contains an unsupported source recipe")
        if not requests:
            return ()
        with locked_session_transaction(self.authority._engine, str(self.inventory.running.claim.session_id)) as conn:
            before = _read_current_sources(conn, selected=self.selected, inventory=self.inventory)
            if policy_sources is not None and policy_sources != before:
                raise AuditIntegrityError("family cohort changed during current policy acquisition")
            messages: list[dict[str, Any]] = []
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
                else:
                    raise AuditIntegrityError("family cohort lost its admitted recipe")
                if family_message_wire(message) != text:
                    raise AuditIntegrityError("family cohort producer differs from complete original selected wire")
                messages.append(message)
            after = _read_current_sources(conn, selected=self.selected, inventory=self.inventory)
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
            return tuple(messages)
