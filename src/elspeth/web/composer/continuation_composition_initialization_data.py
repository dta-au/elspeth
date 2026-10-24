"""Capture actual first-Compose owners; DATA capture grants no SQL/provider action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer._compose_loop_carriers import _AdvisorReviewState, _ComposeIterationState
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack
from elspeth.web.composer.continuation_attachment_history import empty_attachment_history, encode_attachment_history
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_caches import capture_continuation_caches, encode_continuation_caches
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_frontier_chunk, encode_private_chunk
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from elspeth.web.composer.continuation_iteration import (
    capture_continuation_iteration,
    capture_continuation_producer_inputs,
    encode_continuation_iteration,
)
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_progress import initial_continuation_frontier
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers, _ReadyProducerInputs
from elspeth.web.composer.continuation_prompt_recipes import InitialMessageRecipe
from elspeth.web.composer.protocol import retained_operation_context_message
from elspeth.web.composer.recovery_controls import recovery_context_message
from elspeth.web.composer.schema_disclosure import SchemaDisclosureSnapshot
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.composer.turn_effects import TurnEvidence, authored_state_hash
from elspeth.web.sessions.continuation_selection import require_private_chunk_closure, required_private_chunks

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_composition_initialization import CurrentCompositionInitializationSource


@dataclass(frozen=True, slots=True)
class InitialCompositionOwners:
    source: CurrentCompositionInitializationSource
    materialization: CurrentContinuationMessageProducers
    live: _ComposeIterationState
    drained: RecorderDrainCursors
    disclosure: SchemaDisclosureSnapshot
    skill_identity: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class CapturedInitialComposition:
    stack: ContinuationFrameStack
    chunks: tuple[PrivateChunk, ...]


def capture_initial_composition(owners: InitialCompositionOwners) -> CapturedInitialComposition:
    """Pure complete capture, also used inside the publisher's existing transaction.

    No current producer callbacks or engine transactions run from this function.
    The publisher separately re-admits source/current authority on its connection.
    """
    if type(owners) is not InitialCompositionOwners:
        raise AuditIntegrityError("initial composition capture requires its actual owner bundle")
    source, materialization, live = owners.source, owners.materialization, owners.live
    if type(live) is not _ComposeIterationState:
        raise AuditIntegrityError("initial composition capture requires its actual complete iteration-state owner")
    if (
        source.initialized
        or type(materialization) is not CurrentContinuationMessageProducers
        or type(materialization.inputs) is not _ReadyProducerInputs
        or materialization.inputs.source is not source
        or materialization.selected is not source.selected
        or materialization.inventory is not source.inventory
        or type(live.turn_evidence) is not TurnEvidence
        or type(live.recorder) is not BufferingRecorder
        or type(owners.drained) is not RecorderDrainCursors
        or type(live.anti_anchor) is not AntiAnchorTracker
        or type(live.advisor_review_state) is not _AdvisorReviewState
        or type(owners.disclosure) is not SchemaDisclosureSnapshot
        or type(live.llm_messages) is not list
        or type(live.tools) is not list
    ):
        raise AuditIntegrityError("initial composition capture changed its actual live/source owners")
    original, root = source.original, source.selected.stack
    context = source.inventory.running.session_operation_context
    logical = root.logical
    if (
        live.state is not original.initial_state
        or live.initial_version != original.initial_state.version
        or live.current_state_id != logical.base_state_id
        or live.composition_turns_used != 0
        or live.discovery_turns_used != 0
        or live.mutation_success_seen
        or live.last_validation is not None
        or live.last_runtime_preflight is not None
        or live.advisor_calls_used != 0
        or live.repair_turns_used != 0
        or live.advisor_checkpoint_passes_used != 0
        or live.persisted_assistant_message_id is not None
        or live.persisted_assistant_content is not None
        or live.persisted_tool_call_turn
        or live.failed_turn is not None
        or live.advisor_repair_context_introduced
        or live.pending_advisor_elision_indices
        or live.continuation_cache_history is not None
    ):
        raise AuditIntegrityError("initial iteration owner is not the actual original first composition")
    if (
        live.turn_evidence.effects.initial_state is not original.initial_state
        or live.turn_evidence.effects.latest_verified_state is not original.initial_state
        or live.turn_evidence.effects.initial_state_id != logical.base_state_id
        or live.turn_evidence.effects.latest_committed_state_id != logical.base_state_id
        or live.turn_evidence.session_id != logical.session_id
        or live.turn_evidence.operation_id != context.fence.operation_id
        or live.turn_evidence._continuation_current_epoch != context.fence.operation_epoch
        or live.turn_evidence._continuation_actor_user_id != original.snapshot.actor_user_id
        or live.turn_evidence.request_id != logical.original_user_message_id
        or live.turn_evidence.request_hash != stable_hash(original.message)
    ):
        raise AuditIntegrityError("initial composition capture changed its original/current evidence binding")
    reader = live.turn_evidence.transcript._blob_reader
    if type(reader) is not CurrentTranscriptBlobReader or reader.service is not materialization.service or reader.context is not context:
        raise AuditIntegrityError("initial composition lost its actual current transcript blob reader")
    reader.require_for_evidence(live.turn_evidence)
    if live.discovery_cache or live.runtime_preflight_cache:
        raise AuditIntegrityError("first composition cannot hide preexisting cache observations")
    empty_recorder = encode_recorder_snapshot(BufferingRecorder().snapshot_for_continuation(drained=RecorderDrainCursors()))
    audit = encode_recorder_snapshot(live.recorder.snapshot_for_continuation(drained=owners.drained))
    if audit != empty_recorder:
        raise AuditIntegrityError("first composition cannot reset or import a prior recorder")
    producer_inputs = capture_continuation_producer_inputs((), env_ref_names=source.inventory.names)
    if materialization.snapshot_inputs() != producer_inputs:
        raise AuditIntegrityError("first composition has no schema-call producer history")
    produced = materialization._produced
    if not produced or any(
        type(recipe) is not InitialMessageRecipe or recipe.index != index for index, (_, recipe, _) in enumerate(produced)
    ):
        raise AuditIntegrityError("first composition lost its exact initial-message producer positions")
    if any(index >= len(live.llm_messages) or live.llm_messages[index] is not message for index, (message, _, _) in enumerate(produced)):
        raise AuditIntegrityError("first composition substituted a copy of an actual registered initial message")
    expected_tail = []
    if original.recovery_context is not None:
        expected_tail.append({"role": "system", "content": recovery_context_message(original.recovery_context)})
    expected_tail.append({"role": "system", "content": retained_operation_context_message(original.retained_context)})
    if live.llm_messages[len(produced) :] != expected_tail:
        raise AuditIntegrityError("first composition changed original recovery/retained-context message order or bytes")
    snapshot = capture_turn_evidence(
        live.turn_evidence,
        live.llm_messages,
        logical=logical,
        recorder=live.recorder,
        drained=owners.drained,
        env_ref_names=source.inventory.names,
        materialization=materialization,
    )
    evidence = encode_turn_evidence(snapshot, env_ref_names=source.inventory.names)
    transcript = encode_message_shelf(snapshot.transcript)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json(
            {"messages_json": snapshot.transcript.messages_json, "current_indexes": snapshot.transcript.current_indexes}
        ),
        env_ref_names=source.inventory.names,
    )
    tools = encode_private_chunk(codec="composer.tools.v1", canonical=canonical_json(live.tools), env_ref_names=source.inventory.names)
    state = encode_private_chunk(
        codec="composer.state.v1", canonical=original.snapshot.initial_state_json, env_ref_names=source.inventory.names
    )
    caches = encode_continuation_caches(
        capture_continuation_caches(
            discovery=live.discovery_cache,
            preflight=live.runtime_preflight_cache,
            last_preflight=None,
            creator=context,
            prior=None,
            env_ref_names=source.inventory.names,
        ),
        env_ref_names=source.inventory.names,
    )
    iteration = encode_continuation_iteration(
        capture_continuation_iteration(
            producer_inputs=producer_inputs,
            last_validation=None,
            advisor_review=live.advisor_review_state,
            failed_turn=None,
            anti_anchor=live.anti_anchor.snapshot_for_continuation(),
            disclosure=owners.disclosure,
            env_ref_names=source.inventory.names,
        ),
        env_ref_names=source.inventory.names,
    )
    attachments = encode_attachment_history(empty_attachment_history(session_id=logical.session_id, operation_id=logical.operation_id))
    frontier = encode_frontier_chunk(
        initial_continuation_frontier(
            request=original.message,
            original_message_id=logical.original_user_message_id,
            graph_hash=authored_state_hash(original.initial_state),
        )
    )
    sequence = source.inventory.running.claim.segment_sequence
    if type(sequence) is not int or sequence < 1:
        raise AuditIntegrityError("first composition lost its actual positive segment sequence")
    compose = ComposeFrame(
        phase="action_ready",
        bootstrap=source.bootstrap.reference,
        iteration=iteration.reference,
        messages=messages.reference,
        tools=tools.reference,
        initial_state=state.reference,
        state=state.reference,
        evidence=evidence.reference,
        audit_buffer=audit.reference,
        transcript=transcript.reference,
        caches=caches.reference,
        attachment_admissions=attachments.reference,
        initial_version=original.initial_state.version,
        current_state_id=logical.base_state_id,
        slice_sequence=sequence,
        composition_slice=0,
        discovery_slice=0,
        composition_total=0,
        discovery_total=0,
        advisor_calls=0,
        repair_turns=0,
        advisor_checkpoint_passes=0,
        mutation_success_seen=False,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        advisor_repair_context_introduced=False,
        pending_advisor_elision_indices=(),
        pending_response=None,
    )
    following = root.model_copy(update={"frames": (compose,), "semantic_frontier": frontier.reference})
    added = (source.bootstrap, iteration, messages, tools, state, evidence, audit, transcript, caches, attachments, frontier)
    available = {chunk.reference: chunk for chunk in added}
    chunks = tuple(available[ref] for ref in required_private_chunks(following))
    require_private_chunk_closure(following, chunks)
    return CapturedInitialComposition(following, chunks)
