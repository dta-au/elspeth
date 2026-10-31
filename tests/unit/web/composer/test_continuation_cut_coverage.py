"""U-35b: every handoff-safe cut has a successor allowance; in-flight cuts are named.

Design §2.4 (coverage) and §3.4 (validate, classify, then select).
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationFrameStack,
    EffectFrontierEntry,
    EndFrame,
    PrivateChunkRef,
    ToolBatchFrame,
)
from elspeth.web.composer.continuation_advisor import encode_continuation_advisor_invocation
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk
from elspeth.web.composer.continuation_end_values import encode_end_candidate
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from elspeth.web.composer.continuation_next_action import (
    ContinuationCutNotResumable,
    handoff_cut_allowance,
    next_continuation_action_policy,
    require_resumable_cut,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.continuation_selection import nested_private_chunks, required_private_chunks
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.composer.test_continuation_frames import initial_stack
from tests.unit.web.composer.test_continuation_nested_sources import nested_source_values

# Real chunks, so the production selector's complete-closure admission runs.
_CHUNKS: dict[PrivateChunkRef, PrivateChunk] = {}
_EMPTY_STATE = canonical_json(CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).to_dict())


def _retain(chunk: PrivateChunk) -> PrivateChunkRef:
    _CHUNKS[chunk.reference] = chunk
    return chunk.reference


def _ref(codec: str, seed: str) -> PrivateChunkRef:
    if codec == "composer.evidence.v2":
        logical = initial_stack().logical
        # Nominal attribution for a pure structural DATA fixture, never a
        # current SQL claim/Running or permission to dispatch/publish.
        context = SessionOperationContext(
            SessionOperationFence(logical.session_id, "cut-coverage-evidence", "structural-data-only", 1),
            SessionOperationKind.COMPOSE,
        )
        state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
        owner = TurnEvidence(
            session_id=logical.session_id,
            session_operation_context=context,
            request_id=logical.original_user_message_id,
            request=seed,
            initial_state_id=logical.base_state_id,
            initial_state=state,
            actor_user_id="alice",
        )
        messages = [{"role": "user", "content": seed}]
        owner.transcript.begin_messages(messages)
        admitted = capture_turn_evidence(
            owner,
            messages,
            logical=logical,
            recorder=BufferingRecorder(),
            drained=RecorderDrainCursors(),
            env_ref_names=frozenset(),
        )
        for original in admitted.dependency_chunks:
            _retain(original)
        return _retain(encode_turn_evidence(admitted))
    body = _EMPTY_STATE if codec == "composer.state.v1" else canonical_json({"seed": seed})
    return _retain(encode_private_chunk(codec=codec, canonical=body))


def _nested_sources() -> tuple[PrivateChunkRef, PrivateChunkRef]:
    candidate, advisor, leaves = nested_source_values(
        state=_ref("composer.state.v1", "6"),
        evidence=_ref("composer.evidence.v2", "7"),
        transcript=_ref("composer.transcript.v1", "9"),
        iteration=_ref("composer.iteration.v1", "2"),
    )
    for leaf in leaves:
        _retain(leaf)
    return _retain(encode_end_candidate(candidate)), _retain(encode_continuation_advisor_invocation(advisor))


_END_CANDIDATE, _ADVISOR_REVIEW = _nested_sources()


def _selected(stack: ContinuationFrameStack) -> SelectedContinuationState:
    """The stack with its exact breadth-first chunk cohort, as a real selection supplies it."""
    order: list[PrivateChunk] = []
    seen: set[tuple[str, str]] = set()
    pending = list(required_private_chunks(stack))
    while pending:
        reference = pending.pop(0)
        if (reference.codec, reference.digest) in seen:
            continue
        seen.add((reference.codec, reference.digest))
        chunk = _CHUNKS[reference]
        order.append(chunk)
        pending.extend(nested_private_chunks(chunk))
    return SelectedContinuationState("checkpoint", stack, tuple(order))


def _compose(**changes: Any) -> ComposeFrame:
    evidence = _ref("composer.evidence.v2", "7")
    # Select the original recorder locator from the captured closed source;
    # do not create a second independently synthesized audit document.
    recorder = TurnEvidenceSnapshot.model_validate_json(_CHUNKS[evidence].canonical).recorder
    values: dict[str, Any] = {
        "phase": "action_ready",
        "bootstrap": _ref("composer.bootstrap.v1", "1"),
        "iteration": _ref("composer.iteration.v1", "2"),
        "messages": _ref("composer.messages.v1", "3"),
        "tools": _ref("composer.tools.v1", "4"),
        "initial_state": _ref("composer.state.v1", "5"),
        "state": _ref("composer.state.v1", "6"),
        "evidence": evidence,
        "audit_buffer": recorder,
        "transcript": _ref("composer.transcript.v1", "9"),
        "caches": _ref("composer.caches.v1", "a"),
        "attachment_admissions": _ref("composer.attachment-admissions.v1", "b"),
        "initial_version": 1,
        "current_state_id": None,
        "slice_sequence": 2,
        "composition_slice": 0,
        "discovery_slice": 0,
        "composition_total": 3,
        "discovery_total": 1,
        "advisor_calls": 1,
        "repair_turns": 0,
        "advisor_checkpoint_passes": 0,
        "mutation_success_seen": True,
        "persisted_assistant_message_id": None,
        "persisted_assistant_content": None,
        "persisted_tool_call_turn": False,
        "advisor_repair_context_introduced": False,
        "pending_advisor_elision_indices": (),
        "pending_response": None,
    }
    values.update(changes)
    return ComposeFrame(**values)


def _stack(*frames: Any, effects: tuple[EffectFrontierEntry, ...] = (), **changes: Any) -> ContinuationFrameStack:
    return initial_stack().model_copy(
        update={
            "frames": frames,
            "effect_frontier": effects,
            "semantic_frontier": _ref("composer.semantic-frontier.v1", "c"),
            **changes,
        }
    )


def _committed(effect_id: str) -> EffectFrontierEntry:
    return EffectFrontierEntry(effect_id=effect_id, disposition="committed", receipt_id=f"receipt:{effect_id}")


POLICY = minimal_continuation_policy()
_LOCAL_MAXIMUM = max(
    (POLICY.actions.read, POLICY.actions.session_sql, POLICY.actions.blob, POLICY.actions.external), key=lambda action: action.total
)
_ADVISOR = AdvisorFrame(phase="input_ready", review=_ADVISOR_REVIEW, response=None, coverage_binding="coverage")
_TOOL_READY = ToolBatchFrame(
    phase="tool_ready",
    response=_ref("composer.response.v1", "e"),
    accumulator=_ref("composer.tool-batch.v1", "f"),
    next_tool_index=1,
    settled_effect_ids=("tool:0",),
)


def _end(phase: str) -> EndFrame:
    return EndFrame(phase=phase, state=_ref("composer.state.v1", "6"), candidate=_END_CANDIDATE, publication_binding=None)


HANDOFF_SAFE_CUTS = {
    "later-compose": (_stack(_compose(), effects=(_committed("provider:0:0"),)), POLICY.actions.primary),
    "advisor-input": (_stack(_compose(), _ADVISOR), POLICY.actions.advisor),
    "end-review": (_stack(_compose(), _end("review_needed")), POLICY.actions.advisor),
    "end-repair": (_stack(_compose(), _end("repair_needed")), POLICY.actions.primary),
    "end-publication": (_stack(_compose(), _end("publication_ready")), POLICY.actions.session_sql),
    "tool-ready": (_stack(_compose(), _TOOL_READY, effects=(_committed("tool:0"),)), _LOCAL_MAXIMUM),
}


@pytest.mark.parametrize("cut", sorted(HANDOFF_SAFE_CUTS))
def test_every_handoff_safe_cut_has_a_successor_allowance(cut: str) -> None:
    stack, expected = HANDOFF_SAFE_CUTS[cut]
    assert stack.is_handoff_safe
    require_resumable_cut(stack)
    assert handoff_cut_allowance(stack, POLICY) == expected


def test_the_first_compose_iteration_stays_with_its_strict_owner_branch() -> None:
    first = _stack(
        _compose(
            slice_sequence=1,
            composition_total=0,
            discovery_total=0,
            advisor_calls=0,
            mutation_success_seen=False,
        )
    )
    assert handoff_cut_allowance(first, POLICY) is None


IN_FLIGHT = (
    pytest.param(
        _stack(_compose(), effects=(EffectFrontierEntry(effect_id="tool:0", disposition="unknown", receipt_id=None),)),
        "unknown effect",
        id="unknown-effect",
    ),
    pytest.param(
        _stack(
            _compose(phase="awaiting_response", provider_effect_key="provider:0:1", provider_input_sha256="a" * 64),
            effects=(EffectFrontierEntry(effect_id="provider:0:1", disposition="dispatched", receipt_id=None),),
        ),
        "unresolved primary provider effect",
        id="primary-dispatched",
    ),
    pytest.param(
        _stack(
            _compose(phase="awaiting_response", provider_effect_key="provider:0:1", provider_input_sha256="a" * 64),
            effects=(EffectFrontierEntry(effect_id="provider:0:1", disposition="prepared", receipt_id=None),),
        ),
        "unresolved primary provider effect",
        id="primary-prepared",
    ),
    pytest.param(
        _stack(_compose(), _TOOL_READY, effects=(EffectFrontierEntry(effect_id="batch:x:p4", disposition="prepared", receipt_id=None),)),
        "prepared or unknown business effect",
        id="business-prepared",
    ),
    pytest.param(
        _stack(_compose(), _TOOL_READY, effects=(EffectFrontierEntry(effect_id="batch:x:p4", disposition="dispatched", receipt_id=None),)),
        "prepared or unknown business effect",
        id="business-dispatched",
    ),
)


@pytest.mark.parametrize(("stack", "message"), IN_FLIGHT)
def test_selector_classifies_in_flight_shapes(stack: ContinuationFrameStack, message: str) -> None:
    with pytest.raises(ContinuationCutNotResumable, match=message) as caught:
        require_resumable_cut(stack)
    assert caught.value.effect_ids
    assert isinstance(caught.value, AuditIntegrityError)


def test_a_dispatched_title_is_resumable_not_classified() -> None:
    title = initial_stack().model_copy(
        update={"effect_frontier": (EffectFrontierEntry(effect_id="provider:3:0", disposition="dispatched", receipt_id=None),)}
    )
    require_resumable_cut(title)


def test_structural_corruption_stays_a_plain_integrity_failure() -> None:
    corrupt = _stack(_compose(), semantic_frontier=None)
    with pytest.raises(AuditIntegrityError, match="semantic frontier") as caught:
        handoff_cut_allowance(corrupt, POLICY)
    assert type(caught.value) is AuditIntegrityError


@pytest.mark.parametrize("cut", sorted(HANDOFF_SAFE_CUTS))
def test_the_production_selector_sizes_every_handoff_safe_cut(cut: str) -> None:
    stack, expected = HANDOFF_SAFE_CUTS[cut]
    assert next_continuation_action_policy(selected=_selected(stack), policy=POLICY) == expected


@pytest.mark.parametrize(("stack", "message"), IN_FLIGHT)
def test_the_production_selector_classifies_in_flight_cuts_before_selecting(stack: ContinuationFrameStack, message: str) -> None:
    with pytest.raises(ContinuationCutNotResumable, match=message):
        next_continuation_action_policy(selected=_selected(stack), policy=POLICY)
